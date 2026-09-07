"""
Faza 5 — testy matchera (piny zamrozonej specyfikacji + carry-forwards z F2).

Pokrycie:
  1. Wektory pin scoringu B (1.00 tylko identyczny SLD / 0.894 / 0.927 / 0.788)
     + guard q_eff na insercji.
  2. Zestaw recall 100%: qmazon, mazon, arnazon, amaz0n, amazon.co.uk,
     ultrabost, kindel + pin academy.cm (carry-forward: eviction z F2
     nie ma prawa wrocic — brak capu).
  3. Taksonomia Match_Type na wierszach sample'a.
  4. No-cross-class-crowding (carry-forward F2): exacty nie wypieraja
     misspellingow.
  5. Progi zalezne od dlugosci (len 4-5 -> d<=1).
  6. Piny Q4 (filtr potrojnych znakow zakotwiczony) — przeniesione na v2.
  7. E2E run_matcher na syntetycznych CSV: kolumny wejscia przeniesione,
     Score_Custom == FinalScore (most), pelny determinizm (dwa runy
     bajt-w-bajt identyczne).
"""

import logging
from pathlib import Path

import pandas as pd
import pytest

from matcher import pipeline, scoring, symspell

logging.disable(logging.CRITICAL)


def _match_one(keyword: str, address: str, brand: str = "amazon.com"):
    """Pomocnik: pelny tor retrieval+scoring dla jednej pary przez pipeline."""
    records = pipeline.prepare_keyword_records(pd.DataFrame(
        [{"Brand_Domain": brand, "Keyword": keyword}]))
    pipeline.init_worker(records, ["Brand_Domain", "Keyword"])
    chunk = pd.DataFrame([{"address": address}])
    return pipeline.process_chunk(chunk)


# ----------------------------------------------------------------------
# 1. Piny scoringu B (specyfikacja zamrozona)
# ----------------------------------------------------------------------

def test_pin_identical_sld_is_the_only_one():
    rows = _match_one("travel", "travel.com", brand="booking.com")
    assert rows and rows[0]["FinalScore"] == pytest.approx(1.0)
    # antysaturacja: jakikolwiek d>=1 NIE moze dac 1.00
    rows = _match_one("booking", "bookinig.com", brand="booking.com")
    assert rows[0]["FinalScore"] < 1.0


def test_pin_bookinig_0894():
    rows = _match_one("booking", "bookinig.com", brand="booking.com")
    assert rows[0]["Distance"] == 1
    assert rows[0]["FinalScore"] == pytest.approx(0.894, abs=0.001)


def test_pin_aemazon_0927_qwerty_resurrected():
    rows = _match_one("amazon", "aemazon.com")
    row = rows[0]
    assert row["Distance"] == 1
    assert row["QWERTY_Subs"] == 1          # a<->z sasiedzi w skanie pozycyjnym
    assert row["FinalScore"] == pytest.approx(0.927, abs=0.001)


def test_pin_kindle88_0788():
    rows = _match_one("kindle", "kindle88.com")
    row = rows[0]
    assert row["Distance"] == 2
    assert row["FinalScore"] == pytest.approx(0.788, abs=0.001)
    assert row["Match_Type"] == "affix"


def test_qeff_guard_bounds_eff():
    # insercja na froncie przesuwa skan pozycyjny -> q zawyzone; guard tnie do d
    final, q_eff = scoring.final_score("amazon", "zamazon", distance=1,
                                       phonetic_match=False)
    assert q_eff <= 1                       # q_eff = min(q, d)
    base_floor = 1 - 1 / 7                  # eff nie moze spasc ponizej 0.6*d
    assert final <= 1 - 0.6 * 1 / 7 + 1e-9
    assert final >= base_floor - 1e-9


# ----------------------------------------------------------------------
# 2. Zestaw recall (100%) + pin academy.cm
# ----------------------------------------------------------------------

RECALL_SET = ["qmazon.com", "mazon.com", "arnazon.com", "amaz0n.com",
              "amazon.co.uk"]


def test_recall_set_amazon_100pct():
    records = pipeline.prepare_keyword_records(pd.DataFrame(
        [{"Brand_Domain": "amazon.com", "Keyword": "amazon.com"}]))
    pipeline.init_worker(records, ["Brand_Domain", "Keyword"])
    chunk = pd.DataFrame([{"address": a} for a in RECALL_SET])
    rows = pipeline.process_chunk(chunk)
    matched = {r["address"] for r in rows}
    assert matched == set(RECALL_SET)       # w tym qmazon (1. znak!) i co.uk


def test_recall_ultrabost_and_kindel():
    assert _match_one("ultraboost", "ultrabost.com", brand="adidas.com")
    assert _match_one("kindle", "kindel.com")


def test_pin_academy_cm_never_evicted_again():
    # Carry-forward F2: brak capu => exact-other-tld nie wypiera academy.cm
    records = pipeline.prepare_keyword_records(pd.DataFrame(
        [{"Brand_Domain": "academy.com", "Keyword": "academy.com"}]))
    pipeline.init_worker(records, ["Brand_Domain", "Keyword"])
    flood = [{"address": f"academy.com.{cc}"} for cc in
             ("br", "ar", "mx", "co", "pe", "uy", "do", "gt", "bo", "py")]
    chunk = pd.DataFrame(flood + [{"address": "academy.cm"}])
    rows = pipeline.process_chunk(chunk)
    assert any(r["address"] == "academy.cm" for r in rows)
    academy_cm = next(r for r in rows if r["address"] == "academy.cm")
    assert academy_cm["FinalScore"] == pytest.approx(1.0)
    assert academy_cm["Match_Type"] == "exact_other_tld"


# ----------------------------------------------------------------------
# 3. Taksonomia Match_Type
# ----------------------------------------------------------------------

def test_match_type_taxonomy():
    assert _match_one("amazon", "amazon.com")[0]["Match_Type"] == "self"
    assert _match_one("amazon", "amazon.pl")[0]["Match_Type"] == "exact_other_tld"
    assert _match_one("kindle", "kindle88.com")[0]["Match_Type"] == "affix"
    assert _match_one("kindle", "kindel.com")[0]["Match_Type"] == "misspelling"


# ----------------------------------------------------------------------
# 4. No-cross-class-crowding (carry-forward F2)
# ----------------------------------------------------------------------

def test_no_cross_class_crowding_without_cap():
    records = pipeline.prepare_keyword_records(pd.DataFrame(
        [{"Brand_Domain": "amazon.com", "Keyword": "prime"}]))
    pipeline.init_worker(records, ["Brand_Domain", "Keyword"])
    exact_flood = [{"address": f"prime.{tld}"} for tld in
                   ("app", "hr", "ad", "in", "sy", "kr", "net", "org", "io",
                    "co", "me", "tv", "cc", "ws", "gg")]
    chunk = pd.DataFrame(exact_flood + [{"address": "primee.com"},
                                        {"address": "prine.com"}])
    rows = pipeline.process_chunk(chunk)
    types = {r["address"]: r["Match_Type"] for r in rows}
    assert types["primee.com"] == "affix"
    assert types["prine.com"] == "misspelling"      # obecny MIMO 15 exactow
    assert sum(1 for t in types.values() if t == "exact_other_tld") == 15


# ----------------------------------------------------------------------
# 5. Progi zalezne od dlugosci
# ----------------------------------------------------------------------

def test_length_dependent_thresholds():
    assert symspell.keyword_max_distance(4) == 1
    assert symspell.keyword_max_distance(5) == 1
    assert symspell.keyword_max_distance(6) == 2
    # hims (len 4): d=1 wchodzi, d=2 NIE (ochrona krotkich stringow)
    assert _match_one("hims", "hins.com", brand="hims.com")            # d=1
    assert not _match_one("hims", "hxns.com", brand="hims.com")        # d=2


# ----------------------------------------------------------------------
# 6. Piny Q4 na v2 (czyszczenie przeniesione bez zmian semantyki)
# ----------------------------------------------------------------------

def test_q4_triple_char_filter_pins():
    df = pd.DataFrame({"address": ["aaamazon.com", "amazonnn.com", "a--b.com"]})
    cleaned = pipeline.clean_misspellings(df)
    kept = set(cleaned["address"])
    assert "aaamazon.com" not in kept        # start potrojny -> out (zamierzone)
    assert "amazonnn.com" in kept            # potrojny w srodku -> zostaje
    assert "a--b.com" not in kept


# ----------------------------------------------------------------------
# 7. E2E: kontrakt kolumn, most Score_Custom, pelny determinizm
# ----------------------------------------------------------------------

def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    brands = tmp_path / "kw.csv"
    pd.DataFrame([
        {"Brand_Domain": "amazon.com", "Keyword": "kindle",
         "Context_Used": "Yes", "Specificity": "branded", "Vertical": "e-commerce",
         "In_Context": "Yes", "Confidence": "high", "Judge": "not_required"},
        {"Brand_Domain": "booking.com", "Keyword": "booking",
         "Context_Used": "Yes", "Specificity": "brand_core", "Vertical": "travel",
         "In_Context": "Yes", "Confidence": "high", "Judge": "not_required"},
    ]).to_csv(brands, index=False)
    addresses = tmp_path / "zp.csv"
    pd.DataFrame({"address": ["kindel.com", "kindle88.com", "bookinig.com",
                              "amazon.co.uk", "unrelated.com"]}).to_csv(
        addresses, index=False)
    return brands, addresses, tmp_path / "out.csv"


def test_run_matcher_e2e_contract_and_determinism(tmp_path):
    brands, addresses, out = _write_inputs(tmp_path)
    stats = pipeline.run_matcher(str(brands), str(addresses), str(out),
                                 chunksize=2, processes=2)
    assert stats["rows"] >= 3
    df = pd.read_csv(out)
    # kontrakt: rename'y + carry-through kolumn Fazy 4 + most Score_Custom
    assert "Keyword_Misspelling" in df.columns and "Brand" in df.columns
    for col in ("Specificity", "Vertical", "In_Context", "Confidence", "Judge"):
        assert col in df.columns
    assert (df["Score_Custom"] == df["FinalScore"]).all()
    kindel = df[df["Keyword_Misspelling"] == "kindel.com"].iloc[0]
    assert kindel["Specificity"] == "branded" and kindel["Match_Type"] == "misspelling"
    assert "unrelated.com" not in set(df["Keyword_Misspelling"])

    # pelny determinizm: drugi run bajt-w-bajt identyczny
    out2 = tmp_path / "out2.csv"
    pipeline.run_matcher(str(brands), str(addresses), str(out2),
                         chunksize=3, processes=1)
    assert out.read_bytes() == out2.read_bytes()
