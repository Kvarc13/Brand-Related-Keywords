"""
Faza 5 — pipeline matchera: czyszczenie, worker (initializer pattern),
streaming adresow, zapis kanoniczny.

Zasady:
  - Step 3 maksymalizuje recall i produkuje metadane; ZERO polityk
    (Match_Type to czysta etykieta; polityki wylacznie w Fazie 8 / Step 5).
  - Domyslnie BEZ capu top-N (cap byl zrodlem evictions academy.cm
    i cross-class crowdingu z Fazy 2); config top_n_per_pair > 0 przywraca
    limit z kanonicznym porzadkiem, gdyby wolumen kiedys tego wymagal.
  - Wszystkie kolumny wejscia (Specificity, Vertical, In_Context, ...)
    przenoszone do wyjscia z konstrukcji.
  - MOST KONTRAKTOWY do Fazy 8: kolumna Score_Custom = FinalScore
    (Step 5 v1 czyta ja po nazwie do dedupu; jedna metryka end-to-end
    de facto naprawia niespojnosc #23; kolumna znika w Fazie 8).
"""

from __future__ import annotations

import csv
import logging
import time
from multiprocessing import Pool, cpu_count
from pathlib import Path

import pandas as pd

from common.domains import address_matching_sld, registrable_domain, registrable_sld
from matcher import generated, scoring, symspell

logger = logging.getLogger("matcher")

# --- kolumny wejsciowe (kontrakt z Fazy 4) ---
BRAND_DOMAIN_COL = "Brand_Domain"
KEYWORD_COL = "Keyword"
ADDRESS_COL = "address"

OUTPUT_RENAMES = {"address": "Keyword_Misspelling", "Brand_Domain": "Brand"}

SCORE_COLUMNS = ["Match_Type", "Generated_Class", "FinalScore", "Score_Custom", "Distance",
                 "QWERTY_Subs", "TLD_Match", "Phonetic_Match",
                 "Score_JaroWinkler", "Score_Dice"]

# ==============================================================================
# Czyszczenie (semantyka v1 zachowana, w tym pin Q4)
# ==============================================================================

def clean_brands(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.dropna(subset=[BRAND_DOMAIN_COL, KEYWORD_COL], inplace=True)
    df = df[df[BRAND_DOMAIN_COL].str.contains(r"\.")]
    df = df[df[BRAND_DOMAIN_COL].str.len() <= 35]
    return df


def clean_misspellings(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.drop_duplicates(subset=[ADDRESS_COL], inplace=True)
    df.dropna(subset=[ADDRESS_COL], inplace=True)
    df = df[df[ADDRESS_COL].str.len() <= 35]
    df = df[df[ADDRESS_COL].str.match(r"^[a-zA-Z0-9-.]+$")]
    # Q4 (pin): filtr potrojnych znakow ZAKOTWICZONY na starcie — zamierzone.
    df = df[~df[ADDRESS_COL].str.match(r"(.)\1{2,}")]
    df = df[~df[ADDRESS_COL].str.contains("--")]
    return df


def prepare_keyword_records(brands_df: pd.DataFrame) -> list[dict]:
    """Pre-processing keywordow: SLD do matchingu, fonetyka, kolumny brandu."""
    brands_df = clean_brands(brands_df)
    brands_df[KEYWORD_COL] = brands_df[KEYWORD_COL].astype(str).str.strip()
    records: list[dict] = []
    for row in brands_df.to_dict("records"):
        match_sld = registrable_sld(str(row[KEYWORD_COL]).lower())
        if not match_sld:
            continue
        row["Keyword_Match"] = match_sld
        row["Keyword_Phonetic"] = scoring.phonetic_code(match_sld)
        row["Brand_Registrable"] = registrable_domain(str(row[BRAND_DOMAIN_COL]))
        row["Brand_TLD"] = str(row[BRAND_DOMAIN_COL]).lower().rsplit(".", 1)[-1] \
            if "." in str(row[BRAND_DOMAIN_COL]) else ""
        records.append(row)
    return records


# ==============================================================================
# Worker (initializer pattern — indeks budowany RAZ per worker)
# ==============================================================================

_INDEX: dict | None = None
_BRAND_COLS: list[str] | None = None
_GENERATED_MAP: dict | None = None


def init_worker(keyword_records: list[dict], brand_cols: list[str],
                enable_generated: bool = True) -> None:
    global _INDEX, _BRAND_COLS, _GENERATED_MAP
    _INDEX = symspell.build_index(keyword_records)
    _BRAND_COLS = brand_cols
    _GENERATED_MAP = generated.build_candidate_map(keyword_records) \
        if enable_generated else {}


def process_chunk(chunk_df: pd.DataFrame) -> list[dict]:
    """Jeden chunk adresow -> wiersze matchy (pelne, z metadanymi)."""
    assert _INDEX is not None and _BRAND_COLS is not None
    rows: list[dict] = []
    chunk_df = clean_misspellings(chunk_df)
    address_cols = [c for c in chunk_df.columns if c != ADDRESS_COL]

    for address_record in chunk_df.to_dict("records"):
        address_full = str(address_record[ADDRESS_COL])
        address_sld = address_matching_sld(address_full)
        if not address_sld:
            continue  # subdomeny/warianty poza klasami (Q14 odlozone) — skip
        matches = symspell.candidates_for(address_sld, _INDEX)
        generated_hits = _GENERATED_MAP.get(address_sld) or []
        # FIX F6: continue dopiero gdy OBA kanaly puste — wczesniejsze
        # `if not matches: continue` odcinalo probe generacyjny dokladnie
        # dla par, dla ktorych ten kanal istnieje (d > prog fuzzy).
        if not matches and not generated_hits:
            continue
        address_phonetic = scoring.phonetic_code(address_sld)
        address_registrable = registrable_domain(address_full)
        address_tld = address_full.lower().rsplit(".", 1)[-1] \
            if "." in address_full else ""

        # kanal generacyjny (F6): probe wariantow w tym samym przebiegu;
        # dedup z fuzzy w parencie (fuzzy wygrywa — bogatsza taksonomia)
        fuzzy_kw_ids = {idx for idx, _ in matches}
        for keyword_idx, class_name in generated_hits:
            if keyword_idx in fuzzy_kw_ids:
                continue  # para znaleziona przez retrieval — nic do dodania
            keyword_record = _INDEX["records"][keyword_idx]
            keyword_sld = keyword_record["Keyword_Match"]
            import jellyfish as _jf
            distance = _jf.damerau_levenshtein_distance(keyword_sld, address_sld)
            phonetic_match = bool(keyword_record["Keyword_Phonetic"]) and \
                keyword_record["Keyword_Phonetic"] == address_phonetic
            final, q_eff = scoring.final_score(
                keyword_sld, address_sld, distance, phonetic_match)
            jw, dice = scoring.diagnostics(keyword_sld, address_sld)
            row = {col: keyword_record.get(col, "") for col in _BRAND_COLS}
            row["Keyword_Misspelling_Key"] = address_full.lower()
            row[ADDRESS_COL] = address_full
            for col in address_cols:
                row[col] = address_record.get(col, "")
            row.update({
                "Match_Type": "generated",
                "Generated_Class": class_name,
                "FinalScore": round(final, 4),
                "Score_Custom": round(final, 4),
                "Distance": distance,
                "QWERTY_Subs": q_eff,
                "TLD_Match": address_tld == keyword_record["Brand_TLD"],
                "Phonetic_Match": phonetic_match,
                "Score_JaroWinkler": round(jw, 4),
                "Score_Dice": round(dice, 4),
            })
            rows.append(row)

        for keyword_idx, distance in matches:
            keyword_record = _INDEX["records"][keyword_idx]
            keyword_sld = keyword_record["Keyword_Match"]
            phonetic_match = bool(keyword_record["Keyword_Phonetic"]) and \
                keyword_record["Keyword_Phonetic"] == address_phonetic
            final, q_eff = scoring.final_score(
                keyword_sld, address_sld, distance, phonetic_match)
            jw, dice = scoring.diagnostics(keyword_sld, address_sld)
            match_type = scoring.classify_match(
                keyword_sld, address_sld, distance,
                keyword_record["Brand_Registrable"], address_registrable)

            row = {col: keyword_record.get(col, "") for col in _BRAND_COLS}
            row["Keyword_Misspelling_Key"] = address_full.lower()
            row[ADDRESS_COL] = address_full
            for col in address_cols:
                row[col] = address_record.get(col, "")
            row.update({
                "Match_Type": match_type,
                "Generated_Class": "",
                "FinalScore": round(final, 4),
                "Score_Custom": round(final, 4),  # most do Fazy 8
                "Distance": distance,
                "QWERTY_Subs": q_eff,
                "TLD_Match": address_tld == keyword_record["Brand_TLD"],
                "Phonetic_Match": phonetic_match,
                "Score_JaroWinkler": round(jw, 4),
                "Score_Dice": round(dice, 4),
            })
            rows.append(row)
    return rows


# ==============================================================================
# Orkiestracja
# ==============================================================================

def run_matcher(brands_file: str, misspellings_file: str, output_file: str,
                chunksize: int = 200_000, top_n_per_pair: int = 0,
                processes: int | None = None,
                enable_generated: bool = True) -> dict:
    started = time.perf_counter()
    if not Path(brands_file).exists():
        raise SystemExit(f"ERROR: brak {brands_file}")
    if not Path(misspellings_file).exists():
        raise SystemExit(f"ERROR: brak {misspellings_file}")

    brands_df = pd.read_csv(brands_file)
    missing = {BRAND_DOMAIN_COL, KEYWORD_COL} - set(brands_df.columns)
    if missing:
        raise SystemExit(f"ERROR: {brands_file} bez kolumn {missing}")
    brand_cols = list(brands_df.columns)

    keyword_records = prepare_keyword_records(brands_df)
    logger.info("Keywordy po pre-processingu: %d (indeks delecji: budowa per worker)",
                len(keyword_records))

    all_rows: list[dict] = []
    chunk_reader = pd.read_csv(misspellings_file, chunksize=chunksize,
                               on_bad_lines="skip")
    processes = processes or cpu_count()
    chunks_done = 0
    with Pool(processes, initializer=init_worker,
              initargs=(keyword_records, brand_cols, enable_generated)) as pool:
        for chunk_rows in pool.imap_unordered(process_chunk, chunk_reader):
            all_rows.extend(chunk_rows)
            chunks_done += 1
            if chunks_done % 5 == 0:
                logger.info("chunki: %d | matche: %d", chunks_done, len(all_rows))
    logger.info("Skan zakonczony: %d chunkow, %d matchy (raw)",
                chunks_done, len(all_rows))

    # --- opcjonalny cap per (brand, keyword) w porzadku kanonicznym ---
    if top_n_per_pair > 0:
        grouped: dict[tuple, list[dict]] = {}
        for row in all_rows:
            grouped.setdefault((row[BRAND_DOMAIN_COL], row[KEYWORD_COL]),
                               []).append(row)
        all_rows = []
        for pair_rows in grouped.values():
            pair_rows.sort(key=scoring.canonical_sort_key)
            all_rows.extend(pair_rows[:top_n_per_pair])

    # --- porzadek kanoniczny globalnie (pelny determinizm wyjscia) ---
    all_rows.sort(key=lambda r: (str(r[BRAND_DOMAIN_COL]), str(r[KEYWORD_COL]),
                                 *scoring.canonical_sort_key(r)))

    header = [c for c in brand_cols] + [ADDRESS_COL] + \
             [c for c in (all_rows[0].keys() if all_rows else [])
              if c not in brand_cols + [ADDRESS_COL, "Keyword_Misspelling_Key"]
              and c not in SCORE_COLUMNS] + SCORE_COLUMNS
    renamed = [OUTPUT_RENAMES.get(c, c) for c in header]

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(renamed)
        for row in all_rows:
            writer.writerow([row.get(c, "") for c in header])

    elapsed = time.perf_counter() - started
    generated_rows = sum(1 for r in all_rows if r["Match_Type"] == "generated")
    stats = {"rows": len(all_rows), "keywords": len(keyword_records),
             "generated_channel_rows": generated_rows,
             "elapsed_s": round(elapsed, 1)}
    logger.info("Kanal generacyjny (F6): %d par-only-generated "
                "(raport udzialu — prognoza: maly wklad po F5)", generated_rows)
    logger.info("Zapisano %d wierszy do %s w %.1fs", len(all_rows),
                output_file, elapsed)
    return stats
