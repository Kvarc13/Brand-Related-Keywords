#!/usr/bin/env python3
"""
Faza 0 — Golden run harness.

Cel: zamrozic OBECNE zachowanie pipeline'u jako punkt odniesienia przed
jakimkolwiek refaktorem. Zasady:

  1. Zero modyfikacji skryptow krokow — uruchamiane verbatim (subprocess).
     Dowod: SHA-256 kazdego skryptu zapisany w manifescie + kopia w snapshotcie.
  2. Czas kazdego kroku mierzony (baseline wydajnosci pod benchmarki skali).
  3. Po kazdym kroku zadeklarowane pliki wyjsciowe kopiowane do
     fixtures/golden/<tag>/ z SHA-256 i liczba wierszy.
  4. Manifest (metrics.json): skrypty, wejscia, czasy, wyniki — pelne
     pochodzenie snapshotu.
  5. Fail-loud: brak wymaganego wejscia / brakujacy klucz API / niezerowy
     exit / brak wymaganego wyjscia przerywa run z niezerowym kodem.
     Preflight m.in. chroni przed zawieszeniem Step 4.5 na input()
     (uruchamiamy go tylko gdy TargetID.csv istnieje).

Uzycie (z katalogu repo, obok skryptow Keyword_Step*.py):

    python tools/golden_run.py                      # kroki 2,3,4,4.5,5
    python tools/golden_run.py --steps 3,4,4.5,5    # wybrane kroki
    python tools/golden_run.py --steps 1,2,3,4,4.5,5 --tag full
    python tools/golden_run.py --force              # swiadome nadpisanie tagu

Uwagi operacyjne:
  - Step 2 wymaga OPENAI_API_KEY (env lub .env w katalogu repo).
  - Step 4.5 wymaga uruchomienia z wnetrza VPN (endpoint za SSO).
  - Step 1 wymaga Playwright (chromium); Common Crawl dodatkowo AWS/Athena.

Exit code: 0 = snapshot kompletny, 1 = krok/output zawiodl, 2 = preflight/pakiet.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CANONICAL_ORDER = ["1", "2", "3", "4", "4.5", "5"]
DEFAULT_STEPS = "2,3,4,4.5,5"
DEFAULT_TAG = "baseline"
DEFAULT_HASH_CAP_MB = 500

# Zewnetrzne wejscia pipeline'u — hashowane do manifestu dla pochodzenia snapshotu.
EXTERNAL_INPUTS = (
    "Brands.csv",
    "brand_content_final.json",
    "Zeropark_Domain_List.csv",
    "dom_targets_nokey.csv",
)


@dataclass(frozen=True)
class StepSpec:
    """Deklaratywny opis jednego kroku pipeline'u (kontrakt we/wy, nie logika)."""

    key: str
    script: str
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    optional_outputs: tuple[str, ...] = ()
    notes: str = ""


STEPS: dict[str, StepSpec] = {
    "1": StepSpec(
        key="1",
        script="Keyword_Step1_Pipeline_Crawler.py",
        required_inputs=("Brands.csv",),
        required_outputs=("brand_content_final.json",),
        optional_outputs=(
            "failed_brands.csv",
            "Output_part_1-Web-Scraper.json",
            "Output_part_2-Common_crawl.json",
            "Output_part_3-Wayback_machine.json",
            "found_homepage_coordinates.csv",
            "missing_brands.csv",
            "athena_cost_log.csv",
            "orchestrator.log",
        ),
        notes="Wymaga Playwright (chromium); Common Crawl wymaga AWS/Athena (.env: S3_OUTPUT).",
    ),
    "2": StepSpec(
        key="2",
        script="Keyword_Step2_Generate_Keywords_RAG.py",
        required_inputs=("Brands.csv",),
        optional_inputs=("brand_content_final.json",),
        required_env=("OPENAI_API_KEY",),
        required_outputs=("Transformed_Keywords.csv",),
        optional_outputs=("Keyword_Errors.csv",),
        notes="Brak brand_content_final.json = tryb fallback dla WSZYSTKICH brandow (inna jakosc keywordow).",
    ),
    "3": StepSpec(
        key="3",
        script="Keyword_Step3_MisspelingMatcher.py",
        required_inputs=("Transformed_Keywords.csv", "Zeropark_Domain_List.csv"),
        required_outputs=("Keyword_Specific_Misspellings.csv",),
    ),
    "4": StepSpec(
        key="4",
        script="Keyword_Step4__MatchTargetID.py",
        required_inputs=("Keyword_Specific_Misspellings.csv", "dom_targets_nokey.csv"),
        required_outputs=("TargetID.csv",),
    ),
    "4.5": StepSpec(
        key="4.5",
        script="Keyword_Step4_5_By_Target_Consecutive.py",
        required_inputs=("TargetID.csv",),
        required_outputs=("TargetData.csv",),
        notes="Wymaga VPN (endpoint za SSO). Preflight na TargetID.csv zapobiega interaktywnemu input().",
    ),
    "5": StepSpec(
        key="5",
        script="Keyword_Step5__Client_Report_Keywords.py",
        required_inputs=("Keyword_Specific_Misspellings.csv", "TargetData.csv"),
        required_outputs=("Brandable_Domains.csv", "Brandable_Domains-INTERNAL.csv"),
    ),
}


# ----------------------------------------------------------------------
# Funkcje czyste (testowane jednostkowo w tests/test_phase0_golden_tools.py)
# ----------------------------------------------------------------------

def order_steps(keys: list[str]) -> list[str]:
    """Waliduje klucze krokow i zwraca je w kanonicznej kolejnosci pipeline'u."""
    unknown = [k for k in keys if k not in STEPS]
    if unknown:
        raise ValueError(f"Nieznane kroki: {unknown}. Dostepne: {CANONICAL_ORDER}")
    return sorted(set(keys), key=CANONICAL_ORDER.index)


def sha256_file(path: Path, cap_bytes: int | None = None) -> str:
    """SHA-256 pliku; przy cap_bytes pomija hashowanie plikow wiekszych niz cap."""
    size = path.stat().st_size
    if cap_bytes is not None and size > cap_bytes:
        return f"skipped:size_{size}_exceeds_cap_{cap_bytes}"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def count_rows(path: Path) -> int | None:
    """Liczba rekordow: CSV = wiersze danych (csv.reader, bez naglowka),
    JSON = dlugosc listy / liczba kluczy. Inne typy: None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                total = sum(1 for _ in csv.reader(f))
            return max(0, total - 1)
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, (list, dict)):
                return len(data)
    except Exception:
        return None
    return None


def env_has_key(root: Path, name: str) -> bool:
    """Klucz dostepny w os.environ LUB w pliku .env obok skryptow
    (kroki same laduja .env przez dotenv, wiec .env wystarcza)."""
    if os.environ.get(name):
        return True
    env_file = root / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith(f"{name}=") and len(stripped) > len(name) + 1:
                    return True
        except OSError:
            return False
    return False


def preflight(spec: StepSpec, root: Path) -> tuple[list[str], list[str]]:
    """Zwraca (errors, warnings) dla kroku. errors == [] oznacza zgode na start."""
    errors: list[str] = []
    warnings: list[str] = []
    for name in spec.required_inputs:
        if not (root / name).exists():
            errors.append(f"[Step {spec.key}] brak wymaganego wejscia: {name}")
    for name in spec.optional_inputs:
        if not (root / name).exists():
            note = f" — {spec.notes}" if spec.notes else ""
            warnings.append(f"[Step {spec.key}] brak opcjonalnego wejscia: {name}{note}")
    for var in spec.required_env:
        if not env_has_key(root, var):
            errors.append(
                f"[Step {spec.key}] brak {var} (env ani .env) — krok wyprodukowalby "
                f"pusty/bledny output, ktory zatrulby baseline."
            )
    return errors, warnings


def file_record(path: Path, hash_cap: int | None = None, rows_cap: int | None = None) -> dict:
    """Metadane pliku do manifestu: rozmiar, SHA-256, liczba wierszy."""
    size = path.stat().st_size
    record: dict = {"bytes": size, "sha256": sha256_file(path, hash_cap)}
    if rows_cap is None or size <= rows_cap:
        rows = count_rows(path)
        if rows is not None:
            record["rows"] = rows
    return record


# ----------------------------------------------------------------------
# Orkiestracja
# ----------------------------------------------------------------------

def run_step(spec: StepSpec, root: Path, python_exe: str) -> dict:
    """Uruchamia skrypt kroku verbatim, dziedziczac stdio (widoczne paski tqdm)."""
    started = time.perf_counter()
    proc = subprocess.run([python_exe, spec.script], cwd=str(root))
    return {
        "script": spec.script,
        "duration_s": round(time.perf_counter() - started, 3),
        "returncode": proc.returncode,
    }


def snapshot_outputs(spec: StepSpec, root: Path, snap_dir: Path) -> tuple[dict, list[str]]:
    """Kopiuje outputy kroku do snapshotu. Zwraca (rekordy, brakujace_wymagane)."""
    outputs: dict = {}
    missing: list[str] = []
    for name in spec.required_outputs:
        src = root / name
        if not src.exists():
            missing.append(name)
            continue
        shutil.copy2(src, snap_dir / name)
        outputs[name] = file_record(src)
    for name in spec.optional_outputs:
        src = root / name
        if src.exists():
            shutil.copy2(src, snap_dir / name)
            outputs[name] = file_record(src)
    return outputs, missing


def write_metrics(snap_dir: Path, metrics: dict) -> None:
    (snap_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def banner(text: str) -> None:
    line = "=" * 64
    print(f"\n{line}\n  {text}\n{line}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", default=DEFAULT_STEPS, help=f"lista krokow, np. '2,3,4,4.5,5' (default: {DEFAULT_STEPS})")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"nazwa snapshotu w fixtures/golden/ (default: {DEFAULT_TAG})")
    parser.add_argument("--root", default=".", help="katalog ze skryptami Keyword_Step*.py (default: cwd)")
    parser.add_argument("--python", default=sys.executable, help="interpreter do uruchamiania krokow")
    parser.add_argument("--hash-cap-mb", type=int, default=DEFAULT_HASH_CAP_MB,
                        help=f"limit MB dla hashowania/liczenia WEJSC (default: {DEFAULT_HASH_CAP_MB}); outputy zawsze pelne")
    parser.add_argument("--force", action="store_true", help="nadpisz istniejacy snapshot o tym tagu (swiadoma decyzja)")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    try:
        selected = order_steps([k.strip() for k in args.steps.split(",") if k.strip()])
    except ValueError as exc:
        print(f"BLAD: {exc}")
        return 2
    if not selected:
        print("BLAD: pusta lista krokow.")
        return 2

    # Pakiet: wszystkie skrypty wybranych krokow musza istniec ZANIM cokolwiek ruszy.
    missing_scripts = [STEPS[k].script for k in selected if not (root / STEPS[k].script).exists()]
    if missing_scripts:
        print(f"BLAD: brak skryptow w {root}: {missing_scripts}")
        return 2

    snap_dir = root / "fixtures" / "golden" / args.tag
    if snap_dir.exists():
        if not args.force:
            print(
                f"BLAD: snapshot '{args.tag}' juz istnieje ({snap_dir}).\n"
                f"Baseline jest chroniony — nadpisanie wymaga jawnego --force."
            )
            return 2
        shutil.rmtree(snap_dir)
    scripts_dir = snap_dir / "scripts"
    scripts_dir.mkdir(parents=True)

    hash_cap = args.hash_cap_mb * 1024 * 1024

    metrics: dict = {
        "meta": {
            "tag": args.tag,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "argv": sys.argv,
            "root": str(root),
        },
        "scripts": {},
        "inputs": {},
        "steps": {},
        "status": "in_progress",
    }

    banner(f"GOLDEN RUN — tag '{args.tag}', kroki: {', '.join(selected)}")
    if "4.5" in selected:
        print("UWAGA: Step 4.5 wymaga polaczenia VPN (endpoint za SSO).")

    # Pochodzenie: wersje skryptow (hash + kopia) i hashe wejsc zewnetrznych.
    for key in selected:
        script = root / STEPS[key].script
        shutil.copy2(script, scripts_dir / script.name)
        metrics["scripts"][key] = {"file": script.name, "sha256": sha256_file(script)}
    for name in EXTERNAL_INPUTS:
        path = root / name
        if path.exists():
            metrics["inputs"][name] = file_record(path, hash_cap=hash_cap, rows_cap=hash_cap)

    for key in selected:
        spec = STEPS[key]
        banner(f"STEP {key} — {spec.script}")

        errors, warnings = preflight(spec, root)
        for warning in warnings:
            print(f"OSTRZEZENIE: {warning}")
        if errors:
            for error in errors:
                print(f"BLAD: {error}")
            metrics["status"] = f"preflight_failed:step_{key}"
            write_metrics(snap_dir, metrics)
            print(f"\nRun przerwany na preflight kroku {key}. Snapshot NIE jest baseline'em.")
            return 2

        result = run_step(spec, root, args.python)
        result["preflight_warnings"] = warnings
        metrics["steps"][key] = result
        print(f"\nStep {key}: exit={result['returncode']}, czas={result['duration_s']}s")

        if result["returncode"] != 0:
            metrics["status"] = f"step_failed:step_{key}"
            write_metrics(snap_dir, metrics)
            print(f"\nBLAD: Step {key} zakonczyl sie kodem {result['returncode']}. "
                  f"Snapshot NIE jest baseline'em.")
            return 1

        outputs, missing = snapshot_outputs(spec, root, snap_dir)
        result["outputs"] = outputs
        if missing:
            metrics["status"] = f"missing_outputs:step_{key}"
            write_metrics(snap_dir, metrics)
            print(f"\nBLAD: Step {key} nie wyprodukowal wymaganych plikow: {missing}. "
                  f"Snapshot NIE jest baseline'em.")
            return 1
        for name, record in outputs.items():
            rows = record.get("rows")
            rows_txt = f", rows={rows}" if rows is not None else ""
            print(f"  snapshot: {name} ({record['bytes']} B{rows_txt})")

    metrics["status"] = "ok"
    write_metrics(snap_dir, metrics)

    banner("PODSUMOWANIE")
    for key in selected:
        step = metrics["steps"][key]
        print(f"  Step {key:>3}: {step['duration_s']:>9.3f}s  ({step['script']})")
    print(f"\nSnapshot: {snap_dir}")
    print(
        "\nKRYTERIA ODBIORU FAZY 0:\n"
        f"  1. commit fixtures/golden/{args.tag}/ (w tym metrics.json) do repo\n"
        f"  2. sanity: python tools/golden_diff.py --baseline fixtures/golden/{args.tag} "
        f"--candidate fixtures/golden/{args.tag}  -> exit 0\n"
        f"  3. czasy krokow w metrics.json = baseline wydajnosci pod pozniejsze benchmarki"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
