"""
Faza 5 — scoring B ("typo-plausibility") + taksonomia Match_Type.

SPECYFIKACJA ZAMROZONA (Q13/B, wiazaca — piny w testach):
  d      = dystans Damerau-Levenshteina na SLD (z retrievalu)
  q      = liczba POZYCYJNYCH substytucji sasiadow QWERTY
  q_eff  = min(q, d)          # guard: skan pozycyjny po insercji zawyza q;
                              # gwarantuje eff w [0.6*d, d]
  eff    = d - 0.4 * q_eff    # edycja sasiedzka kosztuje 0.6 (wskrzeszona
                              # martwa magnituda macierzy v1)
  base   = 1 - eff / max(len_keyword, len_address)
  final  = 1 - (1-base)*0.85  przy zgodnosci fonetycznej (wybacza 15% luki),
           inaczej base       # fonetyka multiplikatywna: nie wynosi smieci
                              # na szczyt i nie saturuje skali
  TLD_Match — osobna kolumna boolowska POZA formula.
  Jaro-Winkler i Dice — wylacznie diagnostycznie.

WEKTORY PIN: identyczny SLD -> 1.00 (JEDYNY przypadek 1.00);
bookinig->0.894; aemazon->0.927 (a<->z sasiedzi: q_eff=1); kindle88->0.788.

PORZADEK KANONICZNY (heap/dedup/raport — pelny determinizm):
  final malejaco -> d rosnaco -> JaroWinkler malejaco -> adres alfabetycznie.

Match_Type (czyste metadane, ZERO akcji — polityki wylacznie w Fazie 8):
  self            adres == domena brandu (rejestrowalna)
  exact_other_tld identyczny SLD pod innym TLD (d=0)
  affix           SLD zaczyna sie od keyworda + doklejka (combosquat)
  misspelling     reszta (d>=1)
"""

from __future__ import annotations

import jellyfish
from metaphone import doublemetaphone

QWERTY_NEIGHBORS = {
    'q': {'w', 'a', 's'}, 'w': {'q', 'e', 'a', 's', 'd'}, 'e': {'w', 'r', 's', 'd', 'f'},
    'r': {'e', 't', 'd', 'f', 'g'}, 't': {'r', 'y', 'f', 'g', 'h'}, 'y': {'t', 'u', 'g', 'h', 'j'},
    'u': {'y', 'i', 'h', 'j', 'k'}, 'i': {'u', 'o', 'j', 'k', 'l'}, 'o': {'i', 'p', 'k', 'l', ';'},
    'p': {'o', '[', 'l', ';'}, 'a': {'q', 'w', 's', 'z', 'x'}, 's': {'a', 'd', 'w', 'e', 'x', 'z', 'c'},
    'd': {'s', 'f', 'e', 'r', 'x', 'c', 'v'}, 'f': {'d', 'g', 'r', 't', 'c', 'v', 'b'},
    'g': {'f', 'h', 't', 'y', 'v', 'b', 'n'}, 'h': {'g', 'j', 'y', 'u', 'b', 'n', 'm'},
    'j': {'h', 'k', 'u', 'i', 'n', 'm', ','}, 'k': {'j', 'l', 'i', 'o', 'm', ',', '.'},
    'l': {'k', ';', 'o', 'p', ',', '.', '/'}, 'z': {'a', 's', 'x'}, 'x': {'z', 's', 'd', 'c'},
    'c': {'x', 'd', 'f', 'v'}, 'v': {'c', 'f', 'g', 'b'}, 'b': {'v', 'g', 'h', 'n'},
    'n': {'b', 'h', 'j', 'm'}, 'm': {'n', 'j', 'k', ','},
}

QWERTY_EDIT_COST = 0.6          # koszt edycji sasiedzkiej (1 - 0.4)
PHONETIC_FORGIVENESS = 0.85     # fonetyka wybacza 15% pozostalej luki


def qwerty_substitutions(s1: str, s2: str) -> int:
    """Pozycyjny skan substytucji sasiadow QWERTY (aproksymacja — guard w score)."""
    count = 0
    for a, b in zip(s1, s2):
        if a != b and b in QWERTY_NEIGHBORS.get(a, ()):
            count += 1
    return count


def phonetic_code(token: str) -> str:
    try:
        primary, secondary = doublemetaphone(token)
        return primary or secondary or ""
    except Exception:
        return ""


def final_score(keyword_sld: str, address_sld: str, distance: int,
                phonetic_match: bool) -> tuple[float, int]:
    """Zwraca (final, q_eff). Formula zamrozona — patrz naglowek modulu."""
    max_len = max(len(keyword_sld), len(address_sld))
    if max_len == 0:
        return 0.0, 0
    q_eff = min(qwerty_substitutions(keyword_sld, address_sld), distance)
    eff = distance - 0.4 * q_eff
    base = 1.0 - eff / max_len
    if phonetic_match:
        return 1.0 - (1.0 - base) * PHONETIC_FORGIVENESS, q_eff
    return base, q_eff


def classify_match(keyword_sld: str, address_sld: str, distance: int,
                   brand_registrable: str, address_registrable: str) -> str:
    if address_registrable and address_registrable == brand_registrable:
        return "self"
    if distance == 0:
        return "exact_other_tld"
    if address_sld.startswith(keyword_sld) and len(address_sld) > len(keyword_sld):
        return "affix"
    return "misspelling"


def canonical_sort_key(row: dict) -> tuple:
    """final v -> d ^ -> JW v -> adres alfabetycznie (pelny determinizm)."""
    return (-row["FinalScore"], row["Distance"],
            -row["Score_JaroWinkler"], row["Keyword_Misspelling_Key"])


def diagnostics(keyword_sld: str, address_sld: str) -> tuple[float, float]:
    """(Jaro-Winkler, Dice n=2) — kolumny diagnostyczne poza formula."""
    jw = jellyfish.jaro_winkler_similarity(keyword_sld, address_sld)
    dice = _dice(keyword_sld, address_sld)
    return jw, dice


def _dice(s1: str, s2: str, n: int = 2) -> float:
    if not s1 or not s2:
        return 0.0
    grams1 = {s1[i:i + n] for i in range(len(s1) - n + 1)}
    grams2 = {s2[i:i + n] for i in range(len(s2) - n + 1)}
    if not grams1 and not grams2:
        return 1.0
    if not grams1 or not grams2:
        return 0.0
    return 2.0 * len(grams1 & grams2) / (len(grams1) + len(grams2))
