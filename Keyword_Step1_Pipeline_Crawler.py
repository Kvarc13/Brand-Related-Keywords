"""
Keyword_Step1_Pipeline_Crawler.py  (Faza 3 — v2)
================================================
Cienki entrypoint kroku 1. Cala implementacja w pakiecie `crawler/`
(schema v2, jedna bramka jakosci, zrodla per plik, checkpoint/resume).
Oryginal v1 zachowany w legacy/Keyword_Step1_Pipeline_Crawler.v1.py.

Tiery (kazdy dostaje tylko domeny nierozwiazane przez poprzednie):
  plain_http      tani requests + eskalacja wg wzorcow (js/bot/thin)
  web_scraper     Playwright tylko dla eskalowanych; blokada zasobow
  common_crawl    Athena + WARC (wymaga AWS + S3_OUTPUT w .env)
  wayback_machine snapshot z oknem swiezosci

Output: brand_content_final.json (schema v2, kontrakt Step 2 bez zmian),
failed_brands.csv, crawl_checkpoint.jsonl (resume; skasuj dla runu od zera).
Raport pokrycia i A/B vs v1: python tools/crawl_report.py --help
"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

from crawler.orchestrator import run

load_dotenv()  # fix regresji vs v1: S3_OUTPUT/AWS z .env (Common Crawl)

CONFIG = {
    # --- I/O ---
    "brands_file": "Brands.csv",
    "url_column": "Brand",
    "output_json": "brand_content_final.json",
    "failed_csv": "failed_brands.csv",
    "checkpoint_file": "crawl_checkpoint.jsonl",
    "resume": True,

    # --- tiery (kolejnosc = fallback; usun wpis, by wylaczyc zrodlo) ---
    "pipeline_order": ["plain_http", "web_scraper", "common_crawl", "wayback_machine"],

    # --- wspolbieznosc / okna ---
    "http_concurrency": 16,
    "ws_concurrency": 5,
    "wayback_max_age_days": 730,

    # --- kanaly opcjonalne (Q8: domyslnie wlaczone) ---
    "enable_subpages": True,
    "enable_wikidata": True,
    "enable_search_snippet": True,   # fallback-brandy; wymaga: pip install ddgs
}


def main() -> int:
    logging.getLogger("primp").setLevel(logging.WARNING)  # ddgs spam
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler("orchestrator.log"),
                  logging.StreamHandler()],
    )
    try:
        stats = asyncio.run(run(CONFIG))
    except KeyboardInterrupt:
        logging.getLogger("crawler").info(
            "Przerwane — checkpoint zachowany, kolejny run wznowi od tego miejsca.")
        return 130
    return 0 if not stats.get("error") else 2


if __name__ == "__main__":
    sys.exit(main())
