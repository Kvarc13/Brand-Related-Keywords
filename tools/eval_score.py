#!/usr/bin/env python3
"""
Faza 1 — scorer zbioru ewaluacyjnego.

Liczy pokrycie etykietami i precision generacji keywordow wzgledem
oetykietowanego fixtures/eval/keyword_eval.csv. Dziala na DOWOLNYM
Transformed_Keywords.csv — dzis mierzy baseline obecnego promptu,
po Fazie 4 ten sam skrypt mierzy nowa generacje (kryterium:
precision >= baseline).

Definicje:
  precision            = (brand_direct + category_ok) / oetykietowane_wygenerowane
  share_brand_direct   = brand_direct / oetykietowane_wygenerowane
  coverage             = oetykietowane_wygenerowane / wszystkie_wygenerowane
Klucz dopasowania: (Brand, Keyword) case-insensitive. Wiersze eval
nieobecne w danej generacji sa nieaktywne (raportowane liczbowo) —
to naturalne przy niedeterministycznym generatorze.

Uzycie:
    python tools/eval_score.py
    python tools/eval_score.py --keywords Transformed_Keywords.csv --json report.json

Exit: 0 = policzone, 2 = blad strukturalny (brak plikow/kolumn).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

LABELS = ("brand_direct", "category_ok", "reject")
GOOD_LABELS = {"brand_direct", "category_ok"}
UNLABELED_SAMPLE_LIMIT = 20


def read_eval(path: Path) -> tuple[dict[tuple[str, str], str], int, list[str]]:
    """Zwraca (mapa klucz->label dla poprawnych etykiet, liczba pustych, bledne wartosci)."""
    labeled: dict[tuple[str, str], str] = {}
    blank = 0
    invalid: list[str] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Brand", "Keyword", "label"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"BLAD: {path} nie zawiera kolumn {sorted(missing)}. "
                     f"Znalezione: {reader.fieldnames}")
        for row in reader:
            key = (row["Brand"].strip().lower(), row["Keyword"].strip().lower())
            label = (row.get("label") or "").strip()
            if not label:
                blank += 1
            elif label in LABELS:
                labeled[key] = label
            else:
                invalid.append(f"{row['Brand']},{row['Keyword']} -> {label!r}")
    return labeled, blank, invalid


def read_generated(path: Path) -> dict[tuple[str, str], dict]:
    """Zdeduplikowane pary (brand, keyword) z pliku generacji + Context_Used."""
    generated: dict[tuple[str, str], dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Brand_Domain", "Keyword"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"BLAD: {path} nie zawiera kolumn {sorted(missing)}. "
                     f"Znalezione: {reader.fieldnames}")
        for row in reader:
            brand = (row.get("Brand_Domain") or "").strip()
            keyword = (row.get("Keyword") or "").strip()
            if not brand or not keyword:
                continue
            key = (brand.lower(), keyword.lower())
            generated.setdefault(key, {
                "Brand": brand,
                "Keyword": keyword,
                "Context_Used": (row.get("Context_Used") or "").strip(),
                "Vertical": (row.get("Vertical") or "").strip(),
            })
    return generated


def score(labeled: dict[tuple[str, str], str],
          generated: dict[tuple[str, str], dict]) -> dict:
    label_counts: Counter = Counter()
    per_context: dict[str, Counter] = {}
    per_vertical: dict[str, Counter] = {}
    unlabeled: list[dict] = []

    for key, info in generated.items():
        label = labeled.get(key)
        if label is None:
            unlabeled.append(info)
            continue
        label_counts[label] += 1
        per_context.setdefault(info["Context_Used"] or "?", Counter())[label] += 1
        per_vertical.setdefault(info.get("Vertical") or "?", Counter())[label] += 1

    labeled_generated = sum(label_counts.values())
    good = sum(label_counts[l] for l in GOOD_LABELS)
    precision = good / labeled_generated if labeled_generated else None

    return {
        "generated_pairs": len(generated),
        "labeled_generated": labeled_generated,
        "coverage": labeled_generated / len(generated) if generated else None,
        "label_counts": dict(label_counts),
        "precision": precision,
        "share_brand_direct": (label_counts["brand_direct"] / labeled_generated
                               if labeled_generated else None),
        "per_context": {ctx: dict(c) for ctx, c in per_context.items()},
        "per_vertical": {
            vertical: {
                "n": sum(counts.values()),
                "precision": round(sum(counts[l] for l in GOOD_LABELS)
                                   / sum(counts.values()), 3),
                "share_brand_direct": round(counts["brand_direct"]
                                            / sum(counts.values()), 3),
            }
            for vertical, counts in sorted(per_vertical.items())
        },
        "eval_rows_inactive": len(set(labeled) - set(generated)),
        "unlabeled_generated": len(unlabeled),
        "unlabeled_sample": [
            f"{u['Brand']}: {u['Keyword']}"
            for u in sorted(unlabeled, key=lambda x: (x["Brand"].lower(), x["Keyword"].lower()))
        ][:UNLABELED_SAMPLE_LIMIT],
    }


def fmt_ratio(value) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_per_vertical(report: dict) -> None:
    print("\nPer Vertical (n / precision / share_brand_direct):")
    for vertical, m in report.get("per_vertical", {}).items():
        print(f"  {vertical:<28} {m['n']:>4}  {m['precision']:.3f}  "
              f"{m['share_brand_direct']:.3f}")


def render(report: dict, invalid: list[str], blank_labels: int) -> None:
    print(f"Wygenerowane pary (dedup):       {report['generated_pairs']}")
    print(f"Z etykieta:                      {report['labeled_generated']}"
          f"  (coverage: {fmt_ratio(report['coverage'])})")
    print(f"Rozklad etykiet:                 {report['label_counts']}")
    print(f"PRECISION (brand_direct+category_ok): {fmt_ratio(report['precision'])}")
    print(f"Udzial brand_direct:             {fmt_ratio(report['share_brand_direct'])}")
    print(f"Per Context_Used:                {report['per_context']}")
    print(f"Wiersze eval nieaktywne w tej generacji: {report['eval_rows_inactive']}")
    print(f"Puste etykiety w pliku eval:     {blank_labels}")
    if invalid:
        print(f"UWAGA — niepoprawne wartosci label ({len(invalid)}): {invalid[:5]}")
    if report["unlabeled_generated"]:
        print(f"\nWygenerowane BEZ etykiety ({report['unlabeled_generated']}); pierwsze:")
        for item in report["unlabeled_sample"]:
            print(f"  - {item}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval", default="fixtures/eval/keyword_eval.csv")
    parser.add_argument("--keywords", default="fixtures/golden/baseline/Transformed_Keywords.csv")
    parser.add_argument("--json", default=None, help="opcjonalny zapis raportu do JSON")
    parser.add_argument("--per-vertical", action="store_true",
                        help="Faza 9: rozbicie metryk po kolumnie Vertical (F4)")
    parser.add_argument("--gate", type=float, default=None,
                        help="Faza 9: prog precision gate'u po-runowego (np. 0.83)")
    parser.add_argument("--gate-mode", choices=["warn", "fail"], default="warn",
                        help="warn (default) = tylko komunikat; fail = exit 1")
    args = parser.parse_args()

    eval_path = Path(args.eval)
    keywords_path = Path(args.keywords)
    if not eval_path.exists():
        print(f"BLAD: brak {eval_path} — najpierw tools/eval_build.py.")
        return 2
    if not keywords_path.exists():
        print(f"BLAD: brak {keywords_path}.")
        return 2

    labeled, blank_labels, invalid = read_eval(eval_path)
    generated = read_generated(keywords_path)
    report = score(labeled, generated)
    render(report, invalid, blank_labels)
    if args.per_vertical:
        render_per_vertical(report)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"\nRaport JSON: {args.json}")

    # --- Faza 9: gate po-runowy (warn domyslnie, fail dla CI) ---
    if args.gate is not None:
        precision = report["precision"]
        if precision is None:
            print(f"\nGATE: brak oetykietowanych par — prog {args.gate} nieoceniany.")
            return 1 if args.gate_mode == "fail" else 0
        if precision < args.gate:
            print(f"\nGATE {'FAIL' if args.gate_mode == 'fail' else 'WARN'}: "
                  f"precision {precision:.3f} < prog {args.gate}")
            return 1 if args.gate_mode == "fail" else 0
        print(f"\nGATE OK: precision {precision:.3f} >= {args.gate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
