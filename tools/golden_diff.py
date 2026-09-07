#!/usr/bin/env python3
"""
Faza 0 — Golden diff.

Porownuje wyjscia kandydata (katalog roboczy po nowym runie albo inny
snapshot) z zamrozonym baseline'em z fixtures/golden/<tag>/. Tym narzedziem
kazda kolejna faza dowodzi kryterium "golden diff wyjasniony co do wiersza".

Filozofia porownania: VERBATIM.
  - CSV: naglowek (zbior + kolejnosc) oraz multizbior wierszy jako surowych
    stringow (collections.Counter) — niezaleznie od kolejnosci wierszy,
    ale BEZ normalizacji liczb: zmiana "0.85" -> "0.850" to tez zmiana
    zachowania, ktora refaktor musi zadeklarowac swiadomie.
    Roznice zapisywane do <out>/<plik>.added.csv i <plik>.removed.csv.
  - JSON bedacy lista obiektow z kluczem 'domain' (brand_content_final.json):
    roznice zbioru domen + zmiany statusow per domena.
  - Pozostale pliki: porownanie SHA-256.

Zakres: dokladnie pliki obecne w baseline (poza metrics.json i scripts/).
Nowe outputy przyszlych faz trafiaja do NOWEGO baseline'u — nie sa tu zgadywane.

Uzycie:
    python tools/golden_diff.py --baseline fixtures/golden/baseline --candidate .
    python tools/golden_diff.py --baseline fixtures/golden/baseline \
        --candidate fixtures/golden/po_fazie_5 --out diff_faza5

Exit code: 0 = identyczne, 1 = sa roznice, 2 = blad strukturalny.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

SKIP_FILES = {"metrics.json"}
SAMPLE_LIMIT = 10


# ----------------------------------------------------------------------
# Wczytywanie
# ----------------------------------------------------------------------

def read_csv_raw(path: Path) -> tuple[list[str], list[tuple[str, ...]]]:
    """CSV jako (naglowek, wiersze-krotki surowych stringow); utf-8-sig."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    return rows[0], [tuple(row) for row in rows[1:]]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_rows(path: Path, header: list[str], rows: list[tuple[str, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


# ----------------------------------------------------------------------
# Porownania per typ pliku
# ----------------------------------------------------------------------

def compare_csv(base_path: Path, cand_path: Path, out_dir: Path, name: str) -> dict:
    base_header, base_rows = read_csv_raw(base_path)
    cand_header, cand_rows = read_csv_raw(cand_path)
    report: dict = {
        "type": "csv",
        "rows_baseline": len(base_rows),
        "rows_candidate": len(cand_rows),
    }

    if set(base_header) != set(cand_header):
        report["status"] = "schema_differs"
        report["columns_missing_in_candidate"] = sorted(set(base_header) - set(cand_header))
        report["columns_extra_in_candidate"] = sorted(set(cand_header) - set(base_header))
        return report

    header_order_changed = base_header != cand_header
    if header_order_changed:
        # Ten sam zbior kolumn, inna kolejnosc: przestawiamy wiersze kandydata
        # do porzadku baseline'u, zeby diff wierszy pozostal sensowny.
        index = [cand_header.index(col) for col in base_header]
        cand_rows = [
            tuple(row[i] if i < len(row) else "" for i in index) for row in cand_rows
        ]
    report["header_order_changed"] = header_order_changed

    base_counter, cand_counter = Counter(base_rows), Counter(cand_rows)
    removed = list((base_counter - cand_counter).elements())
    added = list((cand_counter - base_counter).elements())
    report["rows_added"] = len(added)
    report["rows_removed"] = len(removed)

    if added or removed or header_order_changed:
        report["status"] = "differs"
        if added:
            added_file = out_dir / f"{name}.added.csv"
            write_rows(added_file, base_header, added)
            report["added_file"] = str(added_file)
            report["sample_added"] = [list(r) for r in added[:SAMPLE_LIMIT]]
        if removed:
            removed_file = out_dir / f"{name}.removed.csv"
            write_rows(removed_file, base_header, removed)
            report["removed_file"] = str(removed_file)
            report["sample_removed"] = [list(r) for r in removed[:SAMPLE_LIMIT]]
    else:
        report["status"] = "identical"
    return report


def compare_json(base_path: Path, cand_path: Path) -> dict:
    report: dict = {"type": "json"}
    try:
        base = json.loads(base_path.read_text(encoding="utf-8"))
        cand = json.loads(cand_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report["status"] = "differs"
        report["note"] = f"nie udalo sie sparsowac JSON ({exc}); porownanie SHA-256"
        report["sha_equal"] = sha256_file(base_path) == sha256_file(cand_path)
        return report

    if json.dumps(base, sort_keys=True) == json.dumps(cand, sort_keys=True):
        report["status"] = "identical"
        return report

    report["status"] = "differs"

    def domain_map(data) -> dict | None:
        if isinstance(data, list) and data and all(
            isinstance(item, dict) and "domain" in item for item in data
        ):
            return {item["domain"]: item.get("status") for item in data}
        return None

    base_map, cand_map = domain_map(base), domain_map(cand)
    if base_map is not None and cand_map is not None:
        added = sorted(set(cand_map) - set(base_map))
        removed = sorted(set(base_map) - set(cand_map))
        changed = sorted(
            (dom, base_map[dom], cand_map[dom])
            for dom in set(base_map) & set(cand_map)
            if base_map[dom] != cand_map[dom]
        )
        report["domains_added"] = len(added)
        report["domains_removed"] = len(removed)
        report["status_changed"] = len(changed)
        report["sample_added"] = added[:SAMPLE_LIMIT]
        report["sample_removed"] = removed[:SAMPLE_LIMIT]
        report["sample_status_changed"] = changed[:SAMPLE_LIMIT]
    return report


def compare_binary(base_path: Path, cand_path: Path) -> dict:
    equal = sha256_file(base_path) == sha256_file(cand_path)
    return {"type": "binary", "status": "identical" if equal else "differs"}


# ----------------------------------------------------------------------
# Orkiestracja porownania
# ----------------------------------------------------------------------

def compare_dirs(baseline: Path, candidate: Path, out_dir: Path) -> dict:
    """Porownuje kazdy plik baseline'u z odpowiednikiem u kandydata."""
    report: dict = {
        "baseline": str(baseline),
        "candidate": str(candidate),
        "files": {},
    }
    identical = True
    for path in sorted(baseline.iterdir()):
        if path.is_dir() or path.name in SKIP_FILES:
            continue
        cand_path = candidate / path.name
        if not cand_path.exists():
            report["files"][path.name] = {"status": "missing_in_candidate"}
            identical = False
            continue
        suffix = path.suffix.lower()
        if suffix == ".csv":
            file_report = compare_csv(path, cand_path, out_dir, path.name)
        elif suffix == ".json":
            file_report = compare_json(path, cand_path)
        else:
            file_report = compare_binary(path, cand_path)
        report["files"][path.name] = file_report
        if file_report["status"] != "identical":
            identical = False
    report["identical"] = identical
    return report


def render(report: dict) -> None:
    print(f"\nGOLDEN DIFF\n  baseline : {report['baseline']}\n  candidate: {report['candidate']}\n")
    if not report["files"]:
        print("  (baseline nie zawiera plikow do porownania)")
    width = max((len(name) for name in report["files"]), default=10)
    for name, file_report in report["files"].items():
        status = file_report["status"]
        details = ""
        if file_report.get("type") == "csv" and status == "differs":
            details = (f"  +{file_report['rows_added']} / -{file_report['rows_removed']} wierszy"
                       + ("  [zmiana kolejnosci kolumn]" if file_report.get("header_order_changed") else ""))
        elif status == "schema_differs":
            details = (f"  brak kolumn: {file_report['columns_missing_in_candidate']}"
                       f"  nadmiar: {file_report['columns_extra_in_candidate']}")
        elif file_report.get("type") == "json" and status == "differs":
            details = (f"  +{file_report.get('domains_added', '?')} / "
                       f"-{file_report.get('domains_removed', '?')} domen, "
                       f"status zmieniony: {file_report.get('status_changed', '?')}")
        print(f"  {name:<{width}}  {status}{details}")
        for sample_key, label in (("sample_added", "+"), ("sample_removed", "-")):
            for row in file_report.get(sample_key, [])[:SAMPLE_LIMIT]:
                print(f"      {label} {row}")
    verdict = "IDENTYCZNE" if report["identical"] else "ROZNICE — kazdy wiersz wymaga swiadomej decyzji"
    print(f"\nWerdykt: {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", required=True, help="katalog snapshotu baseline (fixtures/golden/<tag>)")
    parser.add_argument("--candidate", default=".", help="katalog z outputami kandydata (default: cwd)")
    parser.add_argument("--out", default="golden_diff_report", help="katalog na zrzuty roznic i report.json")
    args = parser.parse_args()

    baseline = Path(args.baseline).resolve()
    candidate = Path(args.candidate).resolve()
    out_dir = Path(args.out).resolve()

    if not baseline.is_dir():
        print(f"BLAD: baseline nie istnieje lub nie jest katalogiem: {baseline}")
        return 2
    if not candidate.is_dir():
        print(f"BLAD: candidate nie istnieje lub nie jest katalogiem: {candidate}")
        return 2

    report = compare_dirs(baseline, candidate, out_dir)
    render(report)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Pelny raport: {out_dir / 'report.json'}")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
