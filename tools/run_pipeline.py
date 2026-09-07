#!/usr/bin/env python3
"""
run_pipeline.py — czysty runner pipeline'u (calosc lub wskazane kroki).

Na podobienstwo golden_run, ale BEZ snapshotu, BEZ metryk i BEZ tagow:
jedyna funkcja = uruchomic kroki po kolei i stanac GLOSNO na bledzie.
Definicje krokow (skrypty, kolejnosc, preflight wejsc) importowane
z golden_run — jedno zrodlo prawdy, zero duplikacji.

Uzycie:
    python tools/run_pipeline.py                      # caly pipeline 1..5
    python tools/run_pipeline.py --steps 2,3,4,4.5,5  # od keywordgen w dol
    python tools/run_pipeline.py --steps 4.5          # pojedynczy krok (VPN!)

Przerwanie/pad w srodku: bezpieczne — ponowne uruchomienie tej samej
komendy wznawia (checkpointy Step 1/2, batch_state, step45_state).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.golden_run import (CANONICAL_ORDER, STEPS, order_steps,  # noqa: E402
                              preflight, run_step)

DEFAULT_STEPS = ",".join(CANONICAL_ORDER)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", default=DEFAULT_STEPS,
                        help=f"lista krokow, np. '2,3,5' (default: {DEFAULT_STEPS})")
    parser.add_argument("--root", default=".",
                        help="katalog ze skryptami Keyword_Step*.py (default: cwd)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter do uruchamiania krokow")
    args = parser.parse_args()

    try:
        keys = order_steps([k.strip() for k in args.steps.split(",") if k.strip()])
    except ValueError as exc:
        print(f"BLAD: {exc}")
        return 2
    root = Path(args.root).resolve()

    # Preflight swiadomy SEKWENCJI (fix: pierwotna wersja zadala istnienia
    # plikow, ktore dopiero powstana w wczesniejszych krokach tego samego
    # przebiegu — skutek przed przyczyna). Twardy stop tylko dla wejsc,
    # ktorych ZADEN wczesniejszy krok sekwencji nie wyprodukuje; reszte
    # zweryfikuje sam krok (glosny pad, wznowienie ta sama komenda).
    produced_upstream: set[str] = set()
    hard_missing: list[str] = []
    for key in keys:
        spec = STEPS[key]
        errors, warnings = preflight(spec, root)
        for error in errors:
            missing_name = error.split(":")[-1].strip()
            if missing_name not in produced_upstream:
                hard_missing.append(error)
        for warning in warnings:
            print(f"UWAGA: {warning}")
        produced_upstream.update(getattr(spec, "required_outputs", ()) or ())
        produced_upstream.update(getattr(spec, "optional_outputs", ()) or ())
    if hard_missing:
        for error in hard_missing:
            print(f"BLAD: {error}")
        return 2

    total_started = time.perf_counter()
    for key in keys:
        spec = STEPS[key]
        print(f"\n{'=' * 64}\n STEP {key} — {spec.script}\n{'=' * 64}")
        result = run_step(spec, root, args.python)
        if result["returncode"] != 0:
            print(f"\nBLAD: Step {key} zakonczyl sie kodem "
                  f"{result['returncode']} po {result['duration_s']}s.")
            print("Napraw przyczyne i uruchom te sama komende ponownie — "
                  "checkpointy wznowia od tego miejsca.")
            return result["returncode"]
        print(f"Step {key}: OK ({result['duration_s']}s)")

    print(f"\n{'=' * 64}\n PIPELINE OK — kroki {','.join(keys)} "
          f"w {time.perf_counter() - total_started:.0f}s\n{'=' * 64}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
