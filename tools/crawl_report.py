#!/usr/bin/env python3
"""
Faza 3 — raport pokrycia kontekstu + A/B dwoch plikow brand_content.

Sieciowy crawl jest niedeterministyczny, wiec weryfikacja Fazy 3 to NIE
golden diff, tylko ten raport: per plik — liczba domen, rozklad zrodel,
pokrycie kazdego pola (% niepustych), srednia dlugosc main_content;
przy dwoch plikach — tabela obok siebie + domeny odzyskane/utracone.
Dziala takze na starym JSON-ie v1 (brakujace pola v2 -> 0%).

Uzycie:
    python tools/crawl_report.py --new brand_content_final.json
    python tools/crawl_report.py --old brand_content_v1.json --new brand_content_final.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

FIELDS = (
    "title", "meta_description", "meta_keywords", "site_name", "lang",
    "h1_headings", "h2_headings", "nav_links", "json_ld_summary",
    "internal_links", "main_content", "subpages", "wikidata", "search_snippet",
)


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        sys.exit(f"BLAD: {path} nie jest lista rekordow")
    return data


def _nonempty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return True


def analyze(records: list[dict]) -> dict:
    total = len(records)
    coverage = {
        field: (sum(1 for r in records if _nonempty(r.get(field))) / total
                if total else 0.0)
        for field in FIELDS
    }
    lengths = [len(r.get("main_content") or "") for r in records]
    return {
        "total": total,
        "sources": dict(Counter(r.get("source", "?") for r in records)),
        "coverage": coverage,
        "avg_main_content_chars": round(sum(lengths) / total, 1) if total else 0.0,
        "domains": {r.get("domain", "") for r in records},
    }


def render(new: dict, old: dict | None) -> None:
    def pct(value: float) -> str:
        return f"{value * 100:5.1f}%"

    print(f"\n{'POLE':<22}{'NEW':>8}" + (f"{'OLD':>8}" if old else ""))
    for field in FIELDS:
        line = f"{field:<22}{pct(new['coverage'][field]):>8}"
        if old:
            line += f"{pct(old['coverage'][field]):>8}"
        print(line)
    print(f"\n{'domen (dobrych)':<22}{new['total']:>8}"
          + (f"{old['total']:>8}" if old else ""))
    print(f"{'sr. main_content':<22}{new['avg_main_content_chars']:>8}"
          + (f"{old['avg_main_content_chars']:>8}" if old else ""))
    print(f"\nzrodla NEW: {new['sources']}")
    if old:
        print(f"zrodla OLD: {old['sources']}")
        recovered = sorted(new["domains"] - old["domains"])
        lost = sorted(old["domains"] - new["domains"])
        print(f"\nOdzyskane vs OLD ({len(recovered)}): {recovered}")
        print(f"Utracone vs OLD  ({len(lost)}): {lost}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--new", required=True, help="nowy brand_content JSON (v2)")
    parser.add_argument("--old", default=None, help="opcjonalnie: stary JSON do A/B")
    args = parser.parse_args()

    new_path = Path(args.new)
    if not new_path.exists():
        print(f"BLAD: brak {new_path}")
        return 2
    new_report = analyze(load(new_path))

    old_report = None
    if args.old:
        old_path = Path(args.old)
        if not old_path.exists():
            print(f"BLAD: brak {old_path}")
            return 2
        old_report = analyze(load(old_path))

    render(new_report, old_report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
