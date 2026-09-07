"""
Faza 4 — tryb Batch API (bulk, -50% kosztu, okno 24h).

Zasada: ZERO drugiej implementacji logiki. Batch tylko prefetchuje
generacje (n na brand, custom_id 'brand||i'); nastepnie kazdy brand
przechodzi przez runner.process_brand z PrefetchedTransport — merge,
walidatory, sedzia (sync, z groundingiem wg Q9) i retry (sync) sa
DOKLADNIE tym samym kodem co tor synchroniczny.

Odpornosc: stan batcha w pliku (batch_state.json) — przerwanie/restart
wznawia polling istniejacego batcha zamiast placic drugi raz. Polling
ograniczony configiem (domyslnie 26 h scienne przy oknie 24 h API).
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common.domains import normalize_domain
from keywordgen import prompts
from keywordgen import runner
from keywordgen.models import GenerationResult, response_format_for
from keywordgen.llm import TransportError

logger = logging.getLogger("keywordgen.batch")

POLL_INTERVAL_S = 30
TERMINAL = {"completed", "failed", "expired", "cancelled"}


def build_requests(brands: list[str], context_lookup: dict[str, dict],
                   config: dict) -> list[dict]:
    requests_out: list[dict] = []
    n = max(1, config.get("self_consistency_n", 2))
    for brand in brands:
        record = context_lookup.get(normalize_domain(brand))
        has_context = runner.is_valid_context(record)
        block = prompts.build_context_block(record) if has_context else None
        messages = prompts.generation_messages(brand, block)
        for i in range(n):
            requests_out.append({
                "custom_id": f"{brand}||{i}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": config.get("model", "gpt-5-mini"),
                    "messages": messages,
                    "reasoning_effort": config.get("reasoning_effort", "minimal"),
                    "response_format": response_format_for(
                        GenerationResult, "keyword_generation"),
                    "max_completion_tokens": 2000,
                },
            })
    return requests_out


def parse_output_line(line: str) -> tuple[str, int, GenerationResult | None, dict]:
    """-> (brand, gen_index, wynik|None, usage). Blad odpowiedzi -> None."""
    entry = json.loads(line)
    brand, _, index = entry.get("custom_id", "||0").rpartition("||")
    response = entry.get("response") or {}
    body = response.get("body") or {}
    usage_raw = body.get("usage") or {}
    usage = {"prompt_tokens": usage_raw.get("prompt_tokens", 0),
             "completion_tokens": usage_raw.get("completion_tokens", 0)}
    if response.get("status_code") != 200:
        return brand, int(index or 0), None, usage
    try:
        content = body["choices"][0]["message"]["content"]
        return brand, int(index or 0), GenerationResult.model_validate_json(content), usage
    except Exception:
        return brand, int(index or 0), None, usage


class PrefetchedTransport:
    """Podaje prefetched generacje; sedzia i ewentualny retry-generate
    delegowane do realnego transportu (sync)."""

    def __init__(self, generations: list[GenerationResult], real_transport):
        self._generations = list(generations)
        self._real = real_transport

    def generate(self, messages):
        if self._generations:
            return self._generations.pop(0), {"prompt_tokens": 0,
                                              "completion_tokens": 0}
        return self._real.generate(messages)  # retry-on-failure -> sync

    def judge(self, messages, grounded):
        return self._real.judge(messages, grounded)


def _load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def run_batch(brands: list[str], config: dict, transport) -> dict:
    from openai import OpenAI
    client = OpenAI()
    state_path = Path(config.get("batch_state_file", "batch_state.json"))
    state = _load_state(state_path)
    context_lookup = runner.load_context_lookup(Path(config["context_json"]))

    # --- submit (albo resume istniejacego batcha) ---
    if not state.get("batch_id"):
        request_lines = build_requests(brands, context_lookup, config)
        jsonl_path = Path(config.get("batch_input_file", "batch_input.jsonl"))
        jsonl_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in request_lines),
            encoding="utf-8")
        uploaded = client.files.create(file=open(jsonl_path, "rb"), purpose="batch")
        batch = client.batches.create(input_file_id=uploaded.id,
                                      endpoint="/v1/chat/completions",
                                      completion_window="24h")
        state = {"batch_id": batch.id, "input_file_id": uploaded.id}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        logger.info("Batch %s wyslany (%d requestow, %d brandow).",
                    batch.id, len(request_lines), len(brands))
    else:
        logger.info("Resume batcha %s ze stanu.", state["batch_id"])

    # --- ograniczony polling ---
    deadline = time.monotonic() + config.get("batch_max_wait_h", 26) * 3600
    while True:
        batch = client.batches.retrieve(state["batch_id"])
        if batch.status in TERMINAL:
            break
        if time.monotonic() > deadline:
            raise TransportError(
                f"Batch {state['batch_id']}: przekroczono max_wait — stan "
                f"zachowany w {state_path}, ponow pozniej (resume).")
        logger.info("Batch %s: %s (%s) — kolejny odczyt za %ds",
                    state["batch_id"], batch.status,
                    getattr(batch, "request_counts", ""), POLL_INTERVAL_S)
        time.sleep(POLL_INTERVAL_S)

    if batch.status != "completed":
        # Fix: martwy batch (expired/failed/cancelled) MUSI skonsumowac stan —
        # inaczej kazdy rerun wiecznie odpytuje trupa. Nastepne uruchomienie
        # sklada swiezy batch automatycznie.
        state_path.unlink(missing_ok=True)
        raise TransportError(
            f"Batch {state['batch_id']}: status {batch.status} — stan "
            f"wyczyszczony, ponowne uruchomienie zlozy NOWY batch.")

    # --- pobranie i zgrupowanie generacji per brand ---
    output_text = client.files.content(batch.output_file_id).text
    per_brand: dict[str, list[GenerationResult]] = {}
    for line in output_text.splitlines():
        if not line.strip():
            continue
        brand, _, result, _usage = parse_output_line(line)
        if result is not None:
            per_brand.setdefault(brand, []).append(result)

    # --- wspolna sciezka: process_brand na PrefetchedTransport ---
    blocklist_stats = _finalize(brands, per_brand, context_lookup, config, transport)
    state_path.unlink(missing_ok=True)  # batch skonsumowany
    return blocklist_stats


def _finalize(brands: list[str], per_brand: dict[str, list[GenerationResult]],
              context_lookup: dict[str, dict], config: dict, transport) -> dict:
    from keywordgen.validators import load_blocklist
    blocklist = load_blocklist(Path(config["blocklist_file"]))
    checkpoint_path = Path(config["checkpoint_file"])
    done = runner.load_done_brands(checkpoint_path) if config.get("resume", True) else {}

    stats = {"total": len(brands), "resumed": len(done), "errors": 0, "retried": 0}

    # Faza 9.1: ogon sedziow/retry po batchu = to samo waskie gardlo co sync
    # -> ta sama pula per-brand (PrefetchedTransport jest per-brand, realny
    # transport wspoldzielony — klient OpenAI thread-safe).
    todo = [b for b in brands if normalize_domain(b) not in done]
    outcomes: dict[str, "runner.BrandOutcome"] = {}
    workers = max(1, int(config.get("workers", 8)))

    def finalize_one(brand: str):
        prefetched = PrefetchedTransport(per_brand.get(brand, []), transport)
        return runner.process_brand(brand,
                                    context_lookup.get(normalize_domain(brand)),
                                    prefetched, blocklist, config)

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(finalize_one, brand): brand for brand in todo}
        for future in as_completed(futures):
            brand = futures[future]
            outcome = future.result()
            runner.append_checkpoint(checkpoint_path, outcome)
            runner.append_cost_log(Path(config["cost_log"]), outcome,
                                   config.get("model", "?") + " (batch)")
            outcomes[normalize_domain(brand)] = outcome
            if outcome.error:
                stats["errors"] += 1
            if outcome.retried:
                stats["retried"] += 1
            completed += 1
            logger.info("batch finalize %d/%d: %s -> %d kw", completed,
                        len(todo), brand, len(outcome.rows))

    all_rows: list[dict] = []
    for brand in brands:
        key = normalize_domain(brand)
        if key in done:
            all_rows.extend(done[key])
        elif key in outcomes:
            all_rows.extend(outcomes[key].rows)

    runner.write_output(all_rows, Path(config["output_csv"]))
    stats["rows"] = len(all_rows)
    return stats
