"""
Faza 5 — retrieval: indeks symetrycznych delecji (SymSpell-style).

Zastepuje pelne O(adresy x keywordy) z prefiltrami zabijajacymi recall
(pierwszy znak wycinal qmazon/mazon; +-2 dlugosci wycinalo amazon.co.uk
przed fixem SLD). Gwarancja: kazda para o dystansie Damerau-Levenshteina
<= progu keyworda JEST kandydatem (twierdzenie o wspolnym wariancie
delecyjnym); kazdy kandydat weryfikowany prawdziwym DL przed scoringiem.

Progi zalezne od dlugosci (decyzja planu): len 4-5 -> d<=1, len 6+ -> d<=2.
Indeks jest maly (dziesiatki keywordow x setki wariantow), wiec przy
multiprocessing przekazywany initializerem raz per worker, nie per chunk.
"""

from __future__ import annotations

import jellyfish

MAX_DELETE_DISTANCE = 2


def keyword_max_distance(length: int) -> int:
    """len 4-5 -> 1; len 6+ -> 2; krotsze (wstrzykniete SLD 1-3) -> 0 (exact)."""
    if length >= 6:
        return 2
    if length >= 4:
        return 1
    return 0


def deletes(token: str, max_distance: int) -> set[str]:
    """Wszystkie warianty tokenu po usunieciu do max_distance znakow (z nim samym)."""
    variants = {token}
    frontier = {token}
    for _ in range(max_distance):
        next_frontier = set()
        for variant in frontier:
            for i in range(len(variant)):
                next_frontier.add(variant[:i] + variant[i + 1:])
        variants |= next_frontier
        frontier = next_frontier
    return variants


def build_index(keyword_records: list[dict]) -> dict:
    """
    keyword_records: [{'Keyword_Match': sld, ...}, ...] (po pre-processingu).
    Zwraca {'variants': {delete_variant: [idx, ...]},
            'records': keyword_records,
            'length_window': (min_len-2, max_len+2)}.
    """
    variants: dict[str, list[int]] = {}
    lengths = []
    for idx, record in enumerate(keyword_records):
        token = record["Keyword_Match"]
        lengths.append(len(token))
        max_d = keyword_max_distance(len(token))
        record["Keyword_Max_Dist"] = max_d
        for variant in deletes(token, max_d):
            variants.setdefault(variant, []).append(idx)
    window = (min(lengths) - MAX_DELETE_DISTANCE,
              max(lengths) + MAX_DELETE_DISTANCE) if lengths else (0, 0)
    return {"variants": variants, "records": keyword_records,
            "length_window": window}


def candidates_for(address_sld: str, index: dict) -> list[tuple[int, int]]:
    """Zwraca [(keyword_idx, dl_distance), ...] dla par przechodzacych prog.
    Bez prefiltrow pierwszego znaku / +-2 — jedyny prefiltr to globalne okno
    dlugosci wynikajace wprost z max dystansu (bezstratne)."""
    low, high = index["length_window"]
    if not (low <= len(address_sld) <= high):
        return []
    candidate_ids: set[int] = set()
    for variant in deletes(address_sld, MAX_DELETE_DISTANCE):
        hit = index["variants"].get(variant)
        if hit:
            candidate_ids.update(hit)
    results: list[tuple[int, int]] = []
    for idx in candidate_ids:
        record = index["records"][idx]
        distance = jellyfish.damerau_levenshtein_distance(
            record["Keyword_Match"], address_sld)
        if distance <= record["Keyword_Max_Dist"]:
            results.append((idx, distance))
    return results
