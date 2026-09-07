"""
Faza 3 — orkiestrator: tiery -> bramka -> enrich -> checkpoint -> final JSON.

Kontrakt jak v1: kazde zrodlo dostaje TYLKO domeny nierozwiazane przez
poprzednie; wynik dobry (bramka jakosci) wypada z puli. Eskalacja HTTP ->
przegladarka dzieje sie naturalnie: rekord 'escalate'/zly nie przechodzi
bramki, wiec domena spada nizej. Nowosci: resume z checkpointu JSONL,
statystyki per-tier i per-powod, wzbogacanie dobrych wynikow (podstrony
tylko dla zrodel zywych, wikidata/snippet dla wszystkich).
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from collections import Counter
from pathlib import Path

from common.domains import normalize_domain
from crawler import checkpoint as ckpt
from crawler import enrich
from crawler.quality import gate_reason, is_result_good
from crawler.sources import (browser_source, commoncrawl_source, http_source,
                             wayback_source)

logger = logging.getLogger("crawler")

LIVE_SOURCES = {"plain_http", "web_scraper"}  # tylko te maja zywa strone dla podstron


def read_brands(path: Path, column: str) -> list[str]:
    if not path.exists():
        logger.error("Brak pliku brandow: %s", path)
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw = [row.get(column, "").strip() for row in csv.DictReader(f)]
    seen: set[str] = set()
    unique: list[str] = []
    for value in raw:
        key = normalize_domain(value)
        if value and key and key not in seen:
            seen.add(key)
            unique.append(value)
    logger.info("Zaladowano %d unikalnych brandow z %s", len(unique), path)
    return unique


async def _run_source(name: str, domains: list[str], config: dict) -> list[dict]:
    if name == "plain_http":
        return http_source.fetch(domains, concurrency=config.get("http_concurrency", 16))
    if name == "web_scraper":
        return await browser_source.fetch(domains,
                                          concurrency=config.get("ws_concurrency", 5))
    if name == "common_crawl":
        return await commoncrawl_source.fetch(domains)
    if name == "wayback_machine":
        return await wayback_source.fetch(
            domains, max_age_days=config.get("wayback_max_age_days", 730))
    raise ValueError(f"Nieznane zrodlo: {name}")


def _enrich_result(result: dict, config: dict, session) -> None:
    if config.get("enable_subpages", True) and result.get("source") in LIVE_SOURCES:
        enrich.fetch_subpages(result, session)
    if config.get("enable_wikidata", True):
        result["wikidata"] = enrich.fetch_wikidata(
            normalize_domain(result.get("domain", "")), session)


def _snippet_result(domain: str, snippet: str) -> dict:
    from crawler.schema import empty_result
    result = empty_result(domain, f"https://{normalize_domain(domain)}", None)
    result["main_content"] = snippet
    result["search_snippet"] = snippet
    result["status"] = "success"
    result["source"] = "search_snippet"
    return result


async def run(config: dict) -> dict:
    """Zwraca statystyki runu (dla testow i podsumowania)."""
    brands = read_brands(Path(config["brands_file"]), config["url_column"])
    if not brands:
        return {"error": "no_brands"}

    checkpoint_path = Path(config["checkpoint_file"])
    good: dict[str, dict] = {}
    if config.get("resume", True):
        good = ckpt.good_results_from(ckpt.load_checkpoint(checkpoint_path))
        if good:
            logger.info("Resume: %d domen juz rozwiazanych w checkpointcie", len(good))

    remaining = [b for b in brands if normalize_domain(b) not in good]
    stats: dict = {"per_source": Counter(), "gate_reasons": Counter(),
                   "resumed": len(good), "total": len(brands)}
    enrich_session = enrich.build_fast_session()

    for source_name in config["pipeline_order"]:
        if not remaining:
            logger.info("[%s] pominiete — wszystko rozwiazane", source_name)
            continue
        logger.info("=== %s: %d domen ===", source_name, len(remaining))
        tier_started = time.perf_counter()
        try:
            results = await _run_source(source_name, remaining, config)
        except Exception as exc:
            logger.error("[%s] zrodlo padlo: %s — domeny ida dalej", source_name, exc)
            continue

        good_in_batch = sum(1 for r in results if is_result_good(r))
        if good_in_batch and (config.get("enable_subpages", True)
                              or config.get("enable_wikidata", True)):
            logger.info("[%s] wzbogacanie %d dobrych wynikow "
                        "(podstrony/wikidata; cicha faza, ~kilka s/domene)...",
                        source_name, good_in_batch)
        enriched = 0
        for result in results:
            result.setdefault("source", source_name)
            if is_result_good(result):
                _enrich_result(result, config, enrich_session)
                enriched += 1
                logger.info("[%s] enrich %d/%d: %s", source_name,
                            enriched, good_in_batch, result.get("domain"))
                key = normalize_domain(result["domain"])
                good[key] = result
                stats["per_source"][source_name] += 1
            else:
                stats["gate_reasons"][f"{source_name}:{gate_reason(result)}"] += 1
            ckpt.append_result(checkpoint_path, result)

        remaining = [b for b in remaining if normalize_domain(b) not in good]
        logger.info("[%s] dobre lacznie: %d | pozostalo: %d | czas tieru: %.1fs",
                    source_name, len(good), len(remaining),
                    time.perf_counter() - tier_started)

    # --- ostatni tier: snippet z wyszukiwarki dla brandow bez zadnego kontekstu
    #     (slabszy kontekst > czysta wiedza modelu; redukuje halucynacje Step 2) ---
    if remaining and config.get("enable_search_snippet", True):
        still_remaining = []
        for domain in remaining:
            snippet = enrich.fetch_search_snippet(normalize_domain(domain))
            if snippet:
                result = _snippet_result(domain, snippet)
                good[normalize_domain(domain)] = result
                stats["per_source"]["search_snippet"] = \
                    stats["per_source"].get("search_snippet", 0) + 1
                ckpt.append_result(checkpoint_path, result)
            else:
                still_remaining.append(domain)
        remaining = still_remaining

    # --- final JSON (kontrakt Step 2 bez zmian) + failed CSV ---
    ordered = [good[normalize_domain(b)] for b in brands
               if normalize_domain(b) in good]
    Path(config["output_json"]).write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Zapisano %d wynikow do %s", len(ordered), config["output_json"])

    if remaining:
        with open(config["failed_csv"], "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Brand", "Status"])
            for domain in remaining:
                writer.writerow([domain, "Failed all crawl methods"])
        logger.warning("%d brandow nieodzyskanych -> %s",
                       len(remaining), config["failed_csv"])

    stats["good"] = len(ordered)
    stats["failed"] = len(remaining)
    stats["per_source"] = dict(stats["per_source"])
    stats["gate_reasons"] = dict(stats["gate_reasons"])

    logger.info("=== PODSUMOWANIE === total=%d good=%d failed=%d resumed=%d",
                stats["total"], stats["good"], stats["failed"], stats["resumed"])
    logger.info("per_source=%s", stats["per_source"])
    logger.info("gate_reasons=%s", stats["gate_reasons"])
    return stats
