"""
Faza 6 — kanal generacyjny (dnstwist-style): permutacje typosquatowe
per keyword + probe na SLD adresow w TYM SAMYM przebiegu streamingu.

Klasy (zatwierdzone planem): omisje, transpozycje, substytucje QWERTY,
homoglify (0<->o, 1<->l, rn<->m, vv<->w), podwojenia, hyphenacja,
vowel-swap, bitsquat. Warianty ograniczone do charsetu domenowego
[a-z0-9-] (bez wiodacego/koncowego '-').

Wartosc marginalna po Fazie 5 (uczciwie): fuzzy d<=2 pokrywa wiekszosc
tych klas dla keywordow len>=6; unikalny wklad kanalu to wzorce d=2 dla
krotkich keywordow (prog fuzzy d<=1), kombinacje d>2 i czesc bitsquatow.
Trafienie niesie PEWNOSC KLASY (Match_Type=generated + Generated_Class),
score liczony normalna formula Q13 (uczciwy ranking, nie sztuczne 1.0
w kolumnie podobienstwa).
"""

from __future__ import annotations

import string

from matcher.scoring import QWERTY_NEIGHBORS

_ALLOWED = set(string.ascii_lowercase + string.digits + "-")
_VOWELS = "aeiou"
_HOMOGLYPH_PAIRS = (("0", "o"), ("o", "0"), ("1", "l"), ("l", "1"),
                    ("rn", "m"), ("m", "rn"), ("vv", "w"), ("w", "vv"))


def _valid(token: str) -> bool:
    return (len(token) >= 3 and set(token) <= _ALLOWED
            and not token.startswith("-") and not token.endswith("-"))


def omissions(token: str) -> set[str]:
    return {token[:i] + token[i + 1:] for i in range(len(token))}


def transpositions(token: str) -> set[str]:
    return {token[:i] + token[i + 1] + token[i] + token[i + 2:]
            for i in range(len(token) - 1) if token[i] != token[i + 1]}


def qwerty_substitutions_perms(token: str) -> set[str]:
    variants = set()
    for i, char in enumerate(token):
        for neighbor in QWERTY_NEIGHBORS.get(char, ()):
            if neighbor in _ALLOWED:
                variants.add(token[:i] + neighbor + token[i + 1:])
    return variants


def homoglyphs(token: str) -> set[str]:
    variants = set()
    for src, dst in _HOMOGLYPH_PAIRS:
        start = 0
        while True:
            idx = token.find(src, start)
            if idx == -1:
                break
            variants.add(token[:idx] + dst + token[idx + len(src):])
            start = idx + 1
    return variants


def doublings(token: str) -> set[str]:
    return {token[:i + 1] + token[i] + token[i + 1:] for i in range(len(token))}


def hyphenations(token: str) -> set[str]:
    return {token[:i] + "-" + token[i:] for i in range(1, len(token))}


def vowel_swaps(token: str) -> set[str]:
    variants = set()
    for i, char in enumerate(token):
        if char in _VOWELS:
            for vowel in _VOWELS:
                if vowel != char:
                    variants.add(token[:i] + vowel + token[i + 1:])
    return variants


def bitsquats(token: str) -> set[str]:
    variants = set()
    for i, char in enumerate(token):
        code = ord(char)
        for bit in range(8):
            flipped = chr(code ^ (1 << bit))
            if flipped in _ALLOWED and flipped != char:
                variants.add(token[:i] + flipped + token[i + 1:])
    return variants


CLASS_GENERATORS = (
    ("omission", omissions),
    ("transposition", transpositions),
    ("qwerty_sub", qwerty_substitutions_perms),
    ("homoglyph", homoglyphs),
    ("doubling", doublings),
    ("hyphenation", hyphenations),
    ("vowel_swap", vowel_swaps),
    ("bitsquat", bitsquats),
)


def generate_variants(token: str) -> dict[str, str]:
    """{wariant: klasa} — pierwsza klasa w kolejnosci CLASS_GENERATORS wygrywa;
    sam token nigdy nie jest wariantem."""
    variants: dict[str, str] = {}
    for class_name, generator in CLASS_GENERATORS:
        for variant in generator(token):
            if variant != token and _valid(variant) and variant not in variants:
                variants[variant] = class_name
    return variants


def build_candidate_map(keyword_records: list[dict]) -> dict[str, list[tuple[int, str]]]:
    """{wariant_sld: [(keyword_idx, klasa), ...]} dla probe podczas streamingu."""
    candidate_map: dict[str, list[tuple[int, str]]] = {}
    for idx, record in enumerate(keyword_records):
        for variant, class_name in generate_variants(record["Keyword_Match"]).items():
            candidate_map.setdefault(variant, []).append((idx, class_name))
    return candidate_map
