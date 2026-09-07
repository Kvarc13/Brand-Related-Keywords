"""
Faza 4 — runner: przeplyw per brand + checkpoint + koszty + kontrakt CSV.

Przeplyw per brand:
  kontekst v2 -> N niezaleznych generacji (self-consistency) -> merge ->
  walidatory/pulapki/blocklista -> inject SLD -> In_Context -> sedzia
  (jeden call per brand: wszystkie CAT + low-conf NAV; grounding gdy brand
  fallback/snippet — Q9) -> priority_fill -> retry-on-failure (JEDNA
  regeneracja z feedbackiem, gdy < min_keywords lub < min_brand_direct
  poza SLD) -> wiersze CSV.

Kontrakt wyjscia: kolumny legacy (Brand_Domain, Keyword, Context_Used)
PIERWSZE i niezmienione (Step 3 przenosi wszystko, Step 5 i eval_score
czytaja po nazwach) + nowe: Specificity, Vertical, In_Context, Confidence,
Judge — konsumowane przez Fazy 5/8/9.
"""

from __future__ import annotations

import csv
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from common.domains import normalize_domain
from keywordgen import prompts
from keywordgen import validators as v
from keywordgen.llm import TransportError

logger = logging.getLogger("keywordgen")

OUTPUT_COLUMNS = ("Brand_Domain", "Keyword", "Context_Used", "Specificity",
                  "Vertical", "In_Context", "Confidence", "Judge")


@dataclass
class BrandOutcome:
    brand: str
    rows: list[dict]
    vertical: str
    context_used: str
    usage: dict
    error: str = ""
    retried: bool = False
    dropped: list[str] = None


def load_context_lookup(json_path: Path) -> dict[str, dict]:
    if not json_path.exists():
        logger.warning("Brak %s — WSZYSTKIE brandy w trybie fallback.", json_path)
        return {}
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return {normalize_domain(r.get("domain", "")): r for r in data if r.get("domain")}


def is_valid_context(record: dict | None) -> bool:
    """Odpowiednik bramki v1 (safeguard): status success + jakikolwiek content."""
    if not record or record.get("status") != "success":
        return False
    combined = " ".join([record.get("title", "") or "",
                         record.get("meta_description", "") or "",
                         record.get("main_content", "") or ""]).strip()
    return bool(combined)


def _judge_tokens(transport, brand: str, candidates: list[v.Candidate],
                  grounded: bool, usage: dict) -> list[v.Candidate]:
    items = v.keywords_needing_judge(candidates, grounded)
    if not items:
        return candidates
    judged_tokens = {i["keyword"] for i in items}
    try:
        result, judge_usage = transport.judge(
            prompts.judge_messages(brand, items), grounded=grounded)
        _add_usage(usage, judge_usage)
        verdicts = {v.normalize_token(verdict.keyword): verdict.accept
                    for verdict in result.verdicts}
    except TransportError as exc:
        logger.warning("[%s] sedzia padl: %s — kandydaci zostaja z Judge=error",
                       brand, str(exc)[:120])
        verdicts = None
    return v.apply_judge_verdicts(candidates, verdicts, judged_tokens)


def _generate_candidates(transport, brand: str, context_block: str | None,
                         n_generations: int, usage: dict) -> tuple[list[v.Candidate], str]:
    generations: list[list[dict]] = []
    vertical = ""
    messages = prompts.generation_messages(brand, context_block)
    for _ in range(max(1, n_generations)):
        result, generation_usage = transport.generate(messages)
        _add_usage(usage, generation_usage)
        if not vertical:
            vertical = result.vertical.strip()
        generations.append([item.model_dump() for item in result.keywords])
    return v.merge_generations(generations), vertical


def process_brand(brand: str, context_record: dict | None, transport,
                  blocklist: set[str], config: dict) -> BrandOutcome:
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    has_context = is_valid_context(context_record)
    grounded = (not has_context) or \
        (context_record or {}).get("source") == "search_snippet"
    context_block = prompts.build_context_block(context_record) if has_context else None
    context_used = "Yes" if has_context else "No (Fallback)"

    def pipeline(candidates: list[v.Candidate]) -> tuple[list[v.Candidate], list[str]]:
        report = v.validate_candidates(candidates, blocklist)
        kept = v.inject_sld(report.kept, brand)
        v.mark_in_context(kept, context_block or "")
        kept = _judge_tokens(transport, brand, kept,
                             grounded and config.get("judge_grounding", True), usage)
        selected = v.priority_fill(kept, config["max_keywords"],
                                   config.get("category_cap_ratio", 0.5))
        dropped = [f"{c.keyword}({c.rejected_reason})" for c in report.dropped]
        return selected, dropped

    try:
        candidates, vertical = _generate_candidates(
            transport, brand, context_block, config.get("self_consistency_n", 2), usage)
        selected, dropped = pipeline(candidates)

        retried = False
        if (len(selected) < config.get("min_keywords", 4)
                or v.brand_direct_count(selected) < config.get("min_brand_direct", 2)):
            retried = True
            feedback = (f"Previous attempt kept only {len(selected)} keywords "
                        f"(dropped: {', '.join(dropped[:10]) or 'none'}). Generate a "
                        f"BETTER set: more brand_core/branded items (owned products, "
                        f"technologies, service names) for {brand}.")
            messages = prompts.generation_messages(brand, context_block)
            messages.append({"role": "user", "content": feedback})
            result, retry_usage = transport.generate(messages)
            _add_usage(usage, retry_usage)
            merged = v.merge_generations(
                [[c.__dict__ for c in candidates],
                 [item.model_dump() for item in result.keywords]])
            # po retry wszystko traktujemy jako pelnopravne kandydaty (union)
            for candidate in merged:
                candidate.confidence = "high" if candidate.confidence == "high" else "low"
            selected, dropped = pipeline(merged)

        rows = [{
            "Brand_Domain": brand,
            "Keyword": c.keyword,
            "Context_Used": context_used,
            "Specificity": c.specificity,
            "Vertical": vertical,
            "In_Context": "Yes" if c.in_context else "No",
            "Confidence": c.confidence,
            "Judge": c.judge,
        } for c in selected]
        return BrandOutcome(brand=brand, rows=rows, vertical=vertical,
                            context_used=context_used, usage=usage,
                            retried=retried, dropped=dropped)
    except (TransportError, Exception) as exc:  # noqa: BLE001 — blad per brand, nie run
        logger.error("[%s] generacja padla: %s", brand, str(exc)[:200])
        return BrandOutcome(brand=brand, rows=[], vertical="",
                            context_used=context_used, usage=usage,
                            error=str(exc)[:200], dropped=[])


def _add_usage(total: dict, delta: dict) -> None:
    total["prompt_tokens"] += delta.get("prompt_tokens", 0)
    total["completion_tokens"] += delta.get("completion_tokens", 0)


# ----------------------------------------------------------------------
# Checkpoint / koszty / zapis
# ----------------------------------------------------------------------

def load_done_brands(checkpoint_path: Path) -> dict[str, list[dict]]:
    done: dict[str, list[dict]] = {}
    if not checkpoint_path.exists():
        return done
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("rows"):  # tylko udane; bledy ponawiane
            done[normalize_domain(entry["brand"])] = entry["rows"]
    return done


def append_checkpoint(checkpoint_path: Path, outcome: BrandOutcome) -> None:
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"brand": outcome.brand, "rows": outcome.rows,
                            "usage": outcome.usage, "error": outcome.error,
                            "dropped": outcome.dropped or []},
                           ensure_ascii=False) + "\n")


def append_cost_log(path: Path, outcome: BrandOutcome, model: str) -> None:
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["Brand", "Model", "Prompt_Tokens",
                             "Completion_Tokens", "Retried", "Error"])
        writer.writerow([outcome.brand, model, outcome.usage["prompt_tokens"],
                         outcome.usage["completion_tokens"],
                         "Yes" if outcome.retried else "No", outcome.error])


def write_output(rows: list[dict], output_path: Path) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def run_sync(brands: list[str], config: dict, transport) -> dict:
    """Tor synchroniczny (runy 'na juz' i domyslny dla malych list)."""
    blocklist = v.load_blocklist(Path(config["blocklist_file"]))
    context_lookup = load_context_lookup(Path(config["context_json"]))
    checkpoint_path = Path(config["checkpoint_file"])

    done = load_done_brands(checkpoint_path) if config.get("resume", True) else {}
    stats = {"total": len(brands), "resumed": len(done), "errors": 0, "retried": 0}

    # Faza 9.1 — wspolbieznosc PER-BRAND (projekt potwierdzony przez
    # wlasciciela): wewnatrz brandu sekwencyjnie (twarde zaleznosci
    # gen->merge->judge->retry), miedzy brandami niezaleznie. Watki robia
    # WYLACZNIE process_brand; cale I/O plikowe (checkpoint/cost log) w
    # watku glownym przy odbiorze -> zero lockow na plikach z konstrukcji.
    todo = [b for b in brands if normalize_domain(b) not in done]
    outcomes: dict[str, "BrandOutcome"] = {}
    workers = max(1, int(config.get("workers", 8)))
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_brand, brand,
                        context_lookup.get(normalize_domain(brand)),
                        transport, blocklist, config): brand
            for brand in todo
        }
        for future in as_completed(futures):
            brand = futures[future]
            outcome = future.result()  # process_brand nie rzuca (blad w outcome)
            append_checkpoint(checkpoint_path, outcome)
            append_cost_log(Path(config["cost_log"]), outcome,
                            config.get("model", "?"))
            outcomes[normalize_domain(brand)] = outcome
            if outcome.error:
                stats["errors"] += 1
            if outcome.retried:
                stats["retried"] += 1
            completed += 1
            dropped_note = ""
            if outcome.dropped:
                dropped_note = f" | dropped: {', '.join(outcome.dropped[:8])}"
                if len(outcome.dropped) > 8:
                    dropped_note += f" (+{len(outcome.dropped) - 8})"
            logger.info("keywordgen %d/%d: %s -> %d kw (retry=%s%s)%s",
                        completed, len(todo), brand, len(outcome.rows),
                        outcome.retried,
                        f", ERROR: {outcome.error}" if outcome.error else "",
                        dropped_note)

    # Finalny CSV skladany w KOLEJNOSCI listy brandow — determinizm wyjscia
    # przezywa niedeterminizm ukonczen puli.
    all_rows: list[dict] = []
    for brand in brands:
        key = normalize_domain(brand)
        if key in done:
            all_rows.extend(done[key])
        elif key in outcomes:
            all_rows.extend(outcomes[key].rows)

    write_output(all_rows, Path(config["output_csv"]))
    stats["rows"] = len(all_rows)
    logger.info("=== keywordgen: %d wierszy, %d brandow (resumed %d, retry %d, "
                "errors %d) -> %s", len(all_rows), len(brands), stats["resumed"],
                stats["retried"], stats["errors"], config["output_csv"])
    return stats
