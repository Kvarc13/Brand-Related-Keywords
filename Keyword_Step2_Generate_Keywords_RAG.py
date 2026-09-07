"""
Keyword_Step2_Generate_Keywords_RAG.py  (Faza 4 — v2)
=====================================================
Cienki entrypoint kroku 2. Implementacja w pakiecie `keywordgen/`
(Structured Outputs, model dwupoziomowy Tier1/Tier2, self-consistency,
sedzia z groundingiem, retry-on-failure, checkpoint, log kosztow).
Oryginal v1 zachowany w legacy/Keyword_Step2_Generate_Keywords_RAG.v1.py.

Tryby (CONFIG["mode"]):
  "sync"  — sekwencyjnie, natychmiast (runy "na juz")
  "batch" — OpenAI Batch API (-50% kosztu, okno 24h; resume batch_state.json)
  "auto"  — batch, gdy brandow > batch_threshold; inaczej sync

Output: Transformed_Keywords.csv — kolumny legacy (Brand_Domain, Keyword,
Context_Used) niezmienione + Specificity, Vertical, In_Context, Confidence,
Judge. Resume: keywordgen_checkpoint.jsonl (skasuj dla runu od zera).
"""

import csv
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from keywordgen import batch as batch_mode          # noqa: E402
from keywordgen import runner                       # noqa: E402
from keywordgen.llm import OpenAITransport          # noqa: E402

CONFIG = {
    # --- model ---
    "model": "gpt-5-mini",
    "reasoning_effort": "minimal",
    "self_consistency_n": 2,      # niezalezne generacje (przeciecie = rdzen)
    "judge_grounding": True,      # web search dla brandow fallback/snippet (Q9)
    "workers": 8,                 # 9.1: rownolegle brandy (wewnatrz brandu
                                  # sekwencyjnie); I/O plikowe w watku glownym

    # --- selekcja ---
    "max_keywords": 8,
    "category_cap_ratio": 0.5,    # Tier2 nigdy > 50% slotow
    "min_keywords": 4,            # ponizej -> retry-on-failure (1 regeneracja)
    "min_brand_direct": 2,        # Tier1 poza SLD ponizej -> retry

    # --- tryb ---
    "mode": "sync",               ## auto|sync|batch
    "batch_threshold": 200,       # auto: batch powyzej tylu brandow

    # --- I/O ---
    "brands_file": "Brands.csv",
    "url_column": "Brand",
    "context_json": "brand_content_final.json",
    "output_csv": "Transformed_Keywords.csv",
    "checkpoint_file": "keywordgen_checkpoint.jsonl",
    "cost_log": "keywordgen_cost_log.csv",
    "blocklist_file": "keywordgen/blocklist_transactional.txt",
    "resume": True,
}


def read_brands(path: str, column: str) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        seen, out = set(), []
        for row in csv.DictReader(f):
            value = row.get(column, "").strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                out.append(value)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # szum per-request SDK zaglusza postep per-brand (klasa 'primp' z F3)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    brands = read_brands(CONFIG["brands_file"], CONFIG["url_column"])
    if not brands:
        logging.error("Brak brandow w %s", CONFIG["brands_file"])
        return 2

    transport = OpenAITransport(model=CONFIG["model"],
                                reasoning_effort=CONFIG["reasoning_effort"])
    mode = CONFIG["mode"]
    if mode == "auto":
        mode = "batch" if len(brands) > CONFIG["batch_threshold"] else "sync"
    logging.info("keywordgen: %d brandow, tryb=%s, model=%s",
                 len(brands), mode, CONFIG["model"])

    if mode == "batch":
        stats = batch_mode.run_batch(brands, CONFIG, transport)
    else:
        stats = runner.run_sync(brands, CONFIG, transport)
    return 0 if stats.get("errors", 0) < len(brands) else 2


if __name__ == "__main__":
    sys.exit(main())
