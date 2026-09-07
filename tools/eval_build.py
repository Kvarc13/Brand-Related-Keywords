#!/usr/bin/env python3
"""
Faza 1 — budowa zbioru ewaluacyjnego keywordow.

Generuje fixtures/eval/keyword_eval.csv z ZAMROZONEGO baseline'u
(fixtures/golden/baseline/Transformed_Keywords.csv), do recznego
oetykietowania przez ekspertow domeny.

Rubryka (3 poziomy, kolumna `label`):
  brand_direct  - keyword jednoznacznie wskazuje ten brand (nazwa, produkt
                  wlasny, technologia); ruch "na wylacznosc".
  category_ok   - termin kategorii, w ktorej brand realnie konkuruje,
                  z intencja zakupowa/nawigacyjna (creaditcard -> issuerzy);
                  user trafia do najwyzszego bidu - dopuszczalne, podrzedne.
  reject        - slownictwo opisowe bez intencji type-in (olfactory),
                  slowa transakcyjne, cudze marki (third-party brand).

Pre-wypelnione sa TYLKO wiersze rozstrzygniete decyzjami planu:
  - kotwice z zatwierdzonego planu (label_source=plan_anchor); wiersze
    graniczne (rewards, ambient) celowo BEZ etykiety - kalibruja prog;
  - SLD kazdego brandu (label_source=sld_definition) - brand_direct
    z definicji celu nawigacyjnego; te wiersze pokrywaja tez przyszly
    wymog Fazy 4 "zawsze wstrzykniety SLD".
Reszta `label` pusta - wypelnia czlowiek. Kolumny `notes` wolne.

Ograniczenie znane i akceptowane: SLD liczony jako pierwszy segment domeny
(dziala dla gołych domen brand.tld z Brands.csv); poprawny tldextract
wchodzi w Fazie 2 do pipeline'u - tu to tylko pomoc etykietujacego.

Ochrona pracy ludzkiej: istniejacy plik eval NIE jest nadpisywany bez
--force, a --force oznacza UTRATE wpisanych etykiet (glosne ostrzezenie).

Uzycie:
    python tools/eval_build.py
    python tools/eval_build.py --keywords fixtures/golden/baseline/Transformed_Keywords.csv \
        --brands Brands.csv --out fixtures/eval/keyword_eval.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

LABELS = ("brand_direct", "category_ok", "reject")

EVAL_COLUMNS = ("Brand", "Keyword", "Context_Used", "source", "label", "label_source", "notes")

# Kotwice zatwierdzone w planie (wersja 6). label == "" -> przypadek graniczny,
# celowo do recznej kalibracji progu.
PLAN_ANCHORS: tuple[tuple[str, str, str], ...] = (
    ("amazon.com", "Kindle", "brand_direct"),
    ("amazon.com", "Alexa", "brand_direct"),
    ("amazon.com", "FireTV", "brand_direct"),
    ("americanexpress.com", "AmericanExpress", "brand_direct"),
    ("americanexpress.com", "platinumcard", "brand_direct"),
    ("americanexpress.com", "creditcard", "category_ok"),
    ("americanexpress.com", "chargecard", "category_ok"),
    ("americanexpress.com", "rewards", ""),
    ("booking.com", "hotels", "category_ok"),
    ("booking.com", "travel", "category_ok"),
    ("booking.com", "accommodation", "category_ok"),
    ("aroma360.com", "fragrance", "category_ok"),
    ("aroma360.com", "olfactory", "reject"),
    ("aroma360.com", "branding", "reject"),
    ("aroma360.com", "ambient", ""),
)


def sld_of(brand: str) -> str:
    """Pierwszy segment golej domeny ('titan.fitness' -> 'titan')."""
    return brand.strip().lower().split(".")[0]


def read_keyword_rows(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Brand_Domain", "Keyword", "Context_Used"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"BLAD: {path} nie zawiera kolumn {sorted(missing)}. "
                     f"Znalezione: {reader.fieldnames}")
        return [row for row in reader if (row.get("Brand_Domain") or "").strip()]


def read_brands(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "Brand" not in (reader.fieldnames or []):
            sys.exit(f"BLAD: {path} nie zawiera kolumny 'Brand'. Znalezione: {reader.fieldnames}")
        return [row["Brand"].strip() for row in reader if (row.get("Brand") or "").strip()]


def build_eval_rows(keyword_rows: list[dict], brands: list[str]) -> list[dict]:
    """Sklada wiersze eval: generacja baseline + SLD kazdego brandu + kotwice planu.

    Klucz deduplikacji: (brand.lower(), keyword.lower()); pierwszy napotkany
    zapis ksztaltu (casing) wygrywa, zrodla sie kumuluja.
    """
    rows: dict[tuple[str, str], dict] = {}

    def upsert(brand: str, keyword: str, context: str, source: str,
               label: str = "", label_source: str = "") -> None:
        key = (brand.strip().lower(), keyword.strip().lower())
        entry = rows.get(key)
        if entry is None:
            entry = {
                "Brand": brand.strip(),
                "Keyword": keyword.strip(),
                "Context_Used": context,
                "source": source,
                "label": "",
                "label_source": "",
                "notes": "",
            }
            rows[key] = entry
        elif source not in entry["source"].split("+"):
            entry["source"] += f"+{source}"
        if label and not entry["label"]:
            entry["label"] = label
            entry["label_source"] = label_source

    # 1) Rzeczywista generacja z baseline'u (zrodlo prawdy o ksztalcie keywordow).
    for row in keyword_rows:
        upsert(row["Brand_Domain"], row["Keyword"], row.get("Context_Used", ""), "baseline_run")

    # 2) SLD kazdego brandu - brand_direct z definicji celu nawigacyjnego.
    for brand in brands:
        upsert(brand, sld_of(brand), "synthetic", "sld_anchor",
               label="brand_direct", label_source="sld_definition")

    # 3) Kotwice zatwierdzone planem (graniczne bez etykiety).
    for brand, keyword, label in PLAN_ANCHORS:
        upsert(brand, keyword, "anchor", "plan_anchor",
               label=label, label_source="plan_anchor" if label else "")

    ordered = sorted(
        rows.values(),
        key=lambda r: (r["Brand"].lower(), r["label"] != "", r["Keyword"].lower()),
    )
    return ordered


def summarize(rows: list[dict]) -> str:
    total = len(rows)
    prefilled = sum(1 for r in rows if r["label"])
    to_label = total - prefilled
    per_brand_todo = Counter(r["Brand"] for r in rows if not r["label"])
    fallback_brands = sorted({
        r["Brand"] for r in rows
        if r["source"].startswith("baseline_run") and r["Context_Used"] != "Yes"
    })
    context_counts = Counter(
        r["Context_Used"] for r in rows if "baseline_run" in r["source"]
    )

    lines = [
        f"Wierszy lacznie: {total}  (pre-wypelnione: {prefilled}, do oetykietowania: {to_label})",
        f"Rozklad Context_Used w generacji: {dict(context_counts)}",
    ]
    if fallback_brands:
        lines.append(
            "PRIORYTET etykietowania (brandy w trybie fallback — kandydaci na sedziego "
            f"z groundingiem w Fazie 4): {fallback_brands}"
        )
    lines.append("Do oetykietowania per brand: "
                 + ", ".join(f"{b}={n}" for b, n in sorted(per_brand_todo.items())))
    return "\n".join(lines)


def sample_brands_per_vertical(rows: list[dict], per_vertical: int) -> set[str]:
    """Deterministyczny (seed=42) wybor N brandow per Vertical."""
    import random
    by_vertical: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        brand = row.get("Brand_Domain", "").strip()
        vertical = (row.get("Vertical") or "?").strip() or "?"
        if brand and (vertical, brand) not in seen:
            seen.add((vertical, brand))
            by_vertical.setdefault(vertical, []).append(brand)
    rng = random.Random(42)
    chosen: set[str] = set()
    for vertical in sorted(by_vertical):
        brands = sorted(by_vertical[vertical])
        rng.shuffle(brands)
        chosen.update(brands[:per_vertical])
    return chosen


def update_eval(keywords_path: Path, out_path: Path,
                sample_per_vertical: int = 0) -> int:
    """Tryb --update (Faza 4): nowe pary z biezacej generacji dopisywane
    z pusta etykieta; istniejace wiersze (i praca czlowieka) NIETYKANE."""
    if not out_path.exists():
        print(f"BLAD: --update wymaga istniejacego {out_path} (najpierw pelny build).")
        return 2
    if not keywords_path.exists():
        print(f"BLAD: brak {keywords_path}.")
        return 2

    with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
        existing = list(csv.DictReader(f))
    known = {(r["Brand"].strip().lower(), r["Keyword"].strip().lower())
             for r in existing}

    all_rows = list(read_keyword_rows(keywords_path))
    sampled: set[str] | None = None
    if sample_per_vertical > 0:
        sampled = sample_brands_per_vertical(all_rows, sample_per_vertical)
        print(f"Probka per-vertical (N={sample_per_vertical}, seed=42): "
              f"{len(sampled)} brandow.")

    added = []
    for row in all_rows:
        brand = row.get("Brand_Domain", "").strip()
        keyword = row.get("Keyword", "").strip()
        if not brand or not keyword:
            continue
        if sampled is not None and brand not in sampled:
            continue
        key = (brand.lower(), keyword.lower())
        if key in known:
            continue
        known.add(key)
        added.append({"Brand": brand, "Keyword": keyword,
                      "Context_Used": row.get("Context_Used", ""),
                      "source": "update", "label": "", "label_source": "",
                      "notes": ""})

    if not added:
        print("Brak nowych par — eval bez zmian.")
        return 0

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_COLUMNS)
        writer.writeheader()
        writer.writerows(existing + added)
    labeled = sum(1 for r in existing if r.get("label", "").strip())
    print(f"Dopisano {len(added)} nowych par do {out_path} "
          f"(istniejace {len(existing)} wierszy nietkniete, w tym {labeled} etykiet).")
    print("Nowe pary maja pusta etykiete — do oznaczenia przed eval_score na pelni.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keywords", default="fixtures/golden/baseline/Transformed_Keywords.csv",
                        help="zamrozony Transformed_Keywords.csv z baseline'u")
    parser.add_argument("--brands", default="Brands.csv", help="lista brandow (kolumna 'Brand')")
    parser.add_argument("--out", default="fixtures/eval/keyword_eval.csv",
                        help="docelowy plik eval do etykietowania")
    parser.add_argument("--force", action="store_true",
                        help="nadpisz istniejacy plik eval (UTRATA wpisanych etykiet!)")
    parser.add_argument("--sample-per-vertical", type=int, default=0, metavar="N",
                        help="Faza 9: z --update dopisuj pary TYLKO dla N "
                             "wylosowanych brandow per Vertical (deterministycznie, "
                             "seed=42) — ~reprezentatywna probka do etykietowania "
                             "zamiast pelnej generacji")
    parser.add_argument("--update", action="store_true",
                        help="Faza 4: dopisz NOWE pary (Brand,Keyword) z --keywords do "
                             "istniejacego evala; etykiety i notatki istniejacych wierszy "
                             "pozostaja nietkniete")
    args = parser.parse_args()

    if args.update and args.force:
        print("BLAD: --update i --force wykluczaja sie.")
        return 2

    keywords_path = Path(args.keywords)
    brands_path = Path(args.brands)
    out_path = Path(args.out)

    if not keywords_path.exists():
        print(f"BLAD: brak {keywords_path} — najpierw golden run (Faza 0).")
        return 2
    if not brands_path.exists():
        print(f"BLAD: brak {brands_path}.")
        return 2
    if args.update:
        return update_eval(Path(args.keywords), out_path,
                           sample_per_vertical=args.sample_per_vertical)

    if out_path.exists() and not args.force:
        print(
            f"BLAD: {out_path} juz istnieje.\n"
            f"Plik eval zawiera prace czlowieka (etykiety) — nadpisanie wymaga --force\n"
            f"i oznacza UTRATE wpisanych etykiet."
        )
        return 2

    keyword_rows = read_keyword_rows(keywords_path)
    brands = read_brands(brands_path)
    eval_rows = build_eval_rows(keyword_rows, brands)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_COLUMNS)
        writer.writeheader()
        writer.writerows(eval_rows)

    print(f"Zapisano szablon eval: {out_path}\n")
    print(summarize(eval_rows))
    print(
        "\nInstrukcja etykietowania:\n"
        f"  - dozwolone wartosci `label`: {', '.join(LABELS)} (puste = jeszcze nieoznaczone)\n"
        "  - pytanie kontrolne brand_direct: czy szukajacy tego terminu naturalnie\n"
        "    wyladuje na stronie TEGO brandu?\n"
        "  - pytanie kontrolne category_ok: czy to termin kategorii, w ktorej brand\n"
        "    realnie konkuruje, z intencja zakupowa/nawigacyjna?\n"
        "  - pre-wypelnione etykiety (plan_anchor, sld_definition) wolno korygowac —\n"
        "    plik nalezy do ekspertow domeny.\n"
        "  - postep i baseline precision: python tools/eval_score.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
