"""
Faza 2 — testy `common/` i wpiec do Step 2/3.

Pinuja:
  1. normalize_domain: fix klasy 'awww.' (strip PREFIKSU www, nie substringa),
     schematy/sciezki/case.
  2. registrable_sld: fix klasy 'co.uk' ORAZ pulapka slow-gTLD
     ('travel'/'hotels'/'fitness' NIE moga zniknac z matchingu).
  3. Zgodnosc kluczy Step 2 <-> brand_content_final.json: dla kazdego brandu
     z REALNEGO Brands.csv nowa normalizacja == stare clean_domain (tozsamosc
     na golych domenach) — warunek zerowego diffu Step 2.
  4. Wpiecie Step 3: modul uzywa funkcji z common (ten sam obiekt).
  5. Q4: filtr potrojnych znakow zakotwiczony na poczatku (decyzja planu).
  6. Kontrakt nazw plikow (common.files) == literaly uzywane dzis.
"""

import csv
from pathlib import Path

import pandas as pd

from common import files
from common.domains import (
    address_matching_sld,
    normalize_domain,
    registrable_domain,
    registrable_sld,
)

# Faza 5: inwarianty Step 3 przeprowadzily sie z entry do pakietu matcher —
# testy pinuja ZACHOWANIE, wiec migruja wiazanie razem z nim (semantyka 1:1).
from matcher import pipeline as step3
from matcher.pipeline import prepare_keyword_records

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# normalize_domain
# ----------------------------------------------------------------------

def test_normalize_domain_strips_scheme_path_case_and_www_prefix():
    assert normalize_domain("https://www.Amazon.com/some/path") == "amazon.com"
    assert normalize_domain("HTTP://Booking.com/") == "booking.com"
    assert normalize_domain("www.foo.com") == "foo.com"
    assert normalize_domain("  thomann.de  ") == "thomann.de"


def test_normalize_domain_awww_fix():
    """Stary Step 1 robil replace('www.','') -> 'awww.com' stawal sie 'a.com'."""
    assert normalize_domain("awww.com") == "awww.com"
    assert normalize_domain("wwwork.example") == "wwwork.example"


def test_normalize_domain_empty_and_nan():
    assert normalize_domain("") == ""
    assert normalize_domain("nan") == ""
    assert normalize_domain(float("nan")) == ""


# ----------------------------------------------------------------------
# registrable_sld / registrable_domain
# ----------------------------------------------------------------------

def test_registrable_sld_simple_and_multilabel_tld():
    assert registrable_sld("amazon.com") == "amazon"
    assert registrable_sld("Booking.com") == "booking"
    assert registrable_sld("titan.fitness") == "titan"
    # Fix klasy co.uk (stary strip_tld: 'amazon.co' -> utrata matcha):
    assert registrable_sld("amazon.co.uk") == "amazon"
    assert registrable_sld("amaz0n.co.uk") == "amaz0n"
    assert registrable_sld("shop.amazon.co.uk") == "amazon"


def test_registrable_sld_tld_word_trap():
    """Slowa bedace gTLD-ami NIE moga zniknac (tldextract: domain='')."""
    for word in ("travel", "hotels", "fitness", "app", "shop"):
        assert registrable_sld(word) == word, word
    assert registrable_sld("Prime") == "prime"
    assert registrable_sld("Kindle") == "kindle"


def test_registrable_sld_pure_suffix_fallback_and_empty():
    assert registrable_sld("co.uk") == "co"      # patologia: nigdy '' dla niepustego
    assert registrable_sld("") == ""
    assert registrable_sld(float("nan")) == ""


def test_registrable_domain():
    assert registrable_domain("shop.amazon.co.uk") == "amazon.co.uk"
    assert registrable_domain("https://www.booking.com/x") == "booking.com"


# ----------------------------------------------------------------------
# Zgodnosc kluczy Step 2 (tozsamosc na realnych brandach)
# ----------------------------------------------------------------------

def test_step2_lookup_keys_identical_on_real_brands():
    """Dla golych domen z Brands.csv nowa normalizacja to identycznosc-lower —
    czyli klucze lookupu do brand_content_final.json bez zmian (diff-neutral)."""
    with open(REPO_ROOT / "Brands.csv", "r", encoding="utf-8-sig", newline="") as f:
        brands = [row["Brand"].strip() for row in csv.DictReader(f) if row.get("Brand")]
    assert len(brands) >= 30
    for brand in brands:
        assert normalize_domain(brand) == brand.lower(), brand


# ----------------------------------------------------------------------
# Wpiecie Step 3 + Q4
# ----------------------------------------------------------------------

def test_step3_uses_common_functions():
    # matcher.pipeline importuje te same obiekty funkcji z common.domains
    from matcher import pipeline
    import common.domains as domains
    assert pipeline.registrable_sld is domains.registrable_sld
    assert pipeline.address_matching_sld is domains.address_matching_sld


def test_address_matching_sld_baseline_scope():
    """Faza 2.1: zakres matchingu = efektywny kontrakt baseline'u + fix co.uk.

    Klasy przypiete po odrzuconej pierwszej weryfikacji (diff_faza2):
    subdomeny floodowaly heap top-30 i wypychaly realne typosquaty
    (utrata academy.cm/amazan.com z raportu), a smieci bez sufiksu byly
    w starym kodzie false-positive'ami exact.
    """
    # fix co.uk — jedyna zamierzona NOWA klasa matchy:
    assert address_matching_sld("amazon.co.uk") == "amazon"
    assert address_matching_sld("amaz0n.co.uk") == "amaz0n"
    assert address_matching_sld("academy.cm") == "academy"      # ccTLD typo zostaje
    # subdomeny (w tym www) -> poza matchingiem (Q14):
    assert address_matching_sld("aws.amazon.com") == ""
    assert address_matching_sld("ww38.slack.amaczon.com") == ""
    assert address_matching_sld("www.amaz0n.com") == ""
    assert address_matching_sld("shop.amazon.co.uk") == ""
    # smieci bez poprawnego sufiksu -> poza (stare false positives):
    assert address_matching_sld("amazon.com9788072942671") == ""
    # gole tokeny -> jak w baseline:
    assert address_matching_sld("amazone") == "amazone"
    # zwykle domeny -> SLD:
    assert address_matching_sld("hote3ls.com") == "hote3ls"
    assert address_matching_sld("titan.fitness") == "titan"


def test_step3_keyword_match_semantics_via_module():
    """Semantyka Keyword_Match po wpieciu: domeny -> SLD, gole slowa bez zmian."""
    df = pd.DataFrame({
        step3.BRAND_DOMAIN_COL: ["booking.com"] * 3,
        step3.KEYWORD_COL: ["Booking.com", "travel", "amazon.co.uk"],
    })
    records = prepare_keyword_records(df)
    assert [r["Keyword_Match"] for r in records] == ["booking", "travel", "amazon"]


def test_q4_triple_char_filter_anchored():
    """DECYZJA Q4: filtr lapie tylko POCZATEK stringa (zamierzone)."""
    df = pd.DataFrame({step3.ADDRESS_COL: [
        "aaamazon.com",   # zaczyna sie potrojnym -> OUT
        "amazonnn.com",   # potrojny w srodku/koncu -> ZOSTAJE (zamierzone)
        "amaz--on.com",   # podwojny myslnik -> OUT (osobny filtr)
        "ok-domain.com",  # ZOSTAJE
    ]})
    kept = set(step3.clean_misspellings(df)[step3.ADDRESS_COL])
    assert kept == {"amazonnn.com", "ok-domain.com"}


def test_step3_clean_misspellings_charset_and_length():
    long_domain = "a" * 36 + ".com"
    df = pd.DataFrame({step3.ADDRESS_COL: [
        "valid-1.com", "bad_underscore.com", "spacja tu.com", long_domain,
    ]})
    kept = set(step3.clean_misspellings(df)[step3.ADDRESS_COL])
    assert kept == {"valid-1.com"}


# ----------------------------------------------------------------------
# Kontrakt nazw plikow
# ----------------------------------------------------------------------

def test_files_contract_matches_current_literals():
    assert files.BRANDS_CSV == "Brands.csv"
    assert files.BRAND_CONTENT_JSON == "brand_content_final.json"
    assert files.TRANSFORMED_KEYWORDS_CSV == "Transformed_Keywords.csv"
    assert files.KEYWORD_ERRORS_CSV == "Keyword_Errors.csv"
    assert files.ZEROPARK_DOMAIN_LIST_CSV == "Zeropark_Domain_List.csv"
    assert files.KEYWORD_MISSPELLINGS_CSV == "Keyword_Specific_Misspellings.csv"
    assert files.DOM_TARGETS_CSV == "dom_targets_nokey.csv"
    assert files.TARGET_ID_CSV == "TargetID.csv"
    assert files.TARGET_DATA_CSV == "TargetData.csv"
    assert files.CLIENT_REPORT_CSV == "Brandable_Domains.csv"
    assert files.INTERNAL_REPORT_CSV == "Brandable_Domains-INTERNAL.csv"
