"""
Faza 3 — checkpoint JSONL + resume (wymog skali: crash na 80% z tysiecy
brandow nie moze oznaczac startu od zera).

Semantyka: kazdy wynik (dobry i zly) dopisywany atomowo jako jedna linia
JSONL natychmiast po uzyskaniu. Resume pomija domeny, ktore maja juz wynik
DOBRY (status success + bramka jakosci); bledy sa ponawiane w kolejnym runie.
Finalny brand_content_final.json skladany na koncu z checkpointu (kontrakt
Step 2 bez zmian).
"""

from __future__ import annotations

import json
from pathlib import Path

from common.domains import normalize_domain
from crawler.quality import is_result_good


def append_result(checkpoint_path: Path, result: dict) -> None:
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def load_checkpoint(checkpoint_path: Path) -> list[dict]:
    """Wczytuje wszystkie wpisy; uszkodzone linie pomija glosno (print, nie crash)."""
    if not checkpoint_path.exists():
        return []
    entries: list[dict] = []
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[checkpoint] pominieta uszkodzona linia {line_no}")
    return entries


def good_results_from(entries: list[dict]) -> dict[str, dict]:
    """{znormalizowana domena: najlepszy dobry wynik} — pierwszy dobry wygrywa."""
    good: dict[str, dict] = {}
    for entry in entries:
        key = normalize_domain(entry.get("domain", ""))
        if key and key not in good and is_result_good(entry):
            good[key] = entry
    return good
