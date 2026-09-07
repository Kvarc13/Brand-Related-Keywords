"""
Keyword_Step4__MatchTargetID.py  (Faza 7 — v2)
==============================================
Lookup misspellingow w dumpie targetow Zeropark.

Zmiany vs v1 (legacy/Keyword_Step4__MatchTargetID.v1.py):
  1. MULTIMAPA (Q1): address -> [(id, feed), ...] — v1 nadpisywal duplikaty
     adresow (target_map[addr] = ...), przez co wszystkie pary poza ostatnia
     ginely CICHO -> brakujacy traffic w 4.5 -> zanizony raport. Teraz
     emitowane sa WSZYSTKIE pary; dedup wyjscia po ID (jak w v1) zostaje.
  2. Progress po BAJTACH (os.path.getsize + file.tell) — v1 czytal 2 GB
     dwukrotnie tylko po to, zeby policzyc wiersze dla paska.
Semantyka normalizacji kluczy jak v1 (lower/strip/BOM — BEZ strip www:
misspellingi to pelne adresy, dump musi byc porownywany 1:1).
"""

from __future__ import annotations

import os
import sys

import pandas as pd
from tqdm import tqdm

TARGETS_FILE = "dom_targets_nokey.csv"
LOOKUP_FILE = "Keyword_Specific_Misspellings.csv"
OUTPUT_FILE = "TargetID.csv"

TARGET_MATCH_COLUMN = "address"
TARGET_ID_COLUMN = "id"
TARGET_FEED_COLUMN = "feed"
LOOKUP_COLUMN = "Keyword_Misspelling"
TRAFFIC_TYPE_VALUE = "Domain"
CHUNK_SIZE = 500_000


def normalize(value) -> str:
    """Jak v1: lower + strip + BOM. CELOWO bez strip 'www.' (klucze 1:1)."""
    return str(value).lstrip("\ufeff").strip().lower()


def load_lookup_keys(file_path: str) -> list[str]:
    df = pd.read_csv(file_path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    if LOOKUP_COLUMN not in df.columns:
        sys.exit(f"ERROR: {file_path} bez kolumny {LOOKUP_COLUMN}. "
                 f"Sa: {list(df.columns)}")
    keys = [normalize(v) for v in df[LOOKUP_COLUMN].dropna()]
    keys = [k for k in keys if k and k != "nan"]
    print(f"Wczytano {len(keys)} kluczy z {LOOKUP_COLUMN}.")
    return keys


def stream_match_targets(file_path: str, wanted: set[str]) -> dict[str, list[tuple]]:
    """Streaming dumpu; zwraca MULTIMAPE {address: [(id, feed), ...]}.
    Progress po bajtach — zero drugiego przebiegu pliku."""
    header = pd.read_csv(file_path, nrows=0, encoding="utf-8-sig")
    header.columns = [c.strip() for c in header.columns]
    required = {TARGET_MATCH_COLUMN, TARGET_ID_COLUMN, TARGET_FEED_COLUMN}
    missing = required - set(header.columns)
    if missing:
        sys.exit(f"ERROR: {file_path} bez kolumn: {missing}")

    total_bytes = os.path.getsize(file_path)
    target_map: dict[str, list[tuple]] = {}
    pairs = 0

    with open(file_path, "rb") as file_obj:
        reader = pd.read_csv(
            file_obj,
            usecols=[TARGET_MATCH_COLUMN, TARGET_ID_COLUMN, TARGET_FEED_COLUMN],
            chunksize=CHUNK_SIZE,
            encoding="utf-8-sig",
        )
        with tqdm(total=total_bytes, desc="Skan targetow", unit="B",
                  unit_scale=True) as pbar:
            for chunk in reader:
                chunk.columns = [c.strip() for c in chunk.columns]
                norm_addr = chunk[TARGET_MATCH_COLUMN].map(normalize)
                keep = norm_addr.isin(wanted)
                for addr, tid, feed in zip(norm_addr[keep],
                                           chunk[TARGET_ID_COLUMN][keep],
                                           chunk[TARGET_FEED_COLUMN][keep]):
                    target_map.setdefault(addr, []).append((tid, feed))
                    pairs += 1
                pbar.update(file_obj.tell() - pbar.n)
                pbar.set_postfix(adresy=len(target_map), pary=pairs)

    print(f"Zbudowano multimape: {len(target_map):,} adresow, {pairs:,} par id/feed.")
    return target_map


def build_output_rows(lookup_keys: list[str],
                      target_map: dict[str, list[tuple]]) -> tuple[list, int]:
    """WSZYSTKIE pary (id, feed) per adres (Q1); dedup po ID w kolejnosci
    lookupu (jak v1). Zwraca (rows, liczba_kluczy_bez_matcha)."""
    rows: list[list] = []
    seen_ids: set = set()
    unmatched = 0
    for key in lookup_keys:
        entries = target_map.get(key)
        if not entries:
            unmatched += 1
            continue
        for tid, feed in entries:
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            rows.append([tid, feed, TRAFFIC_TYPE_VALUE])
    return rows, unmatched


def main() -> int:
    for path in (TARGETS_FILE, LOOKUP_FILE):
        if not os.path.exists(path):
            sys.exit(f"ERROR: brak pliku: {path}")

    lookup_keys = load_lookup_keys(LOOKUP_FILE)
    wanted = set(lookup_keys)
    print(f"{len(wanted):,} unikalnych adresow do lookupu.")

    target_map = stream_match_targets(TARGETS_FILE, wanted)
    rows, unmatched = build_output_rows(lookup_keys, target_map)
    multi = sum(1 for entries in target_map.values() if len(entries) > 1)
    print(f"Matched {len(rows):,} unikalnych ID; {unmatched:,} kluczy bez matcha; "
          f"{multi:,} adresow z >1 para id/feed (zysk multimapy).")

    pd.DataFrame(rows, columns=["Generic ID", "Publisher Feed Hash",
                                "Traffic Type"]).to_csv(
        OUTPUT_FILE, index=False, encoding="utf-8")
    print(f"Zapisano {len(rows)} wierszy do {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
