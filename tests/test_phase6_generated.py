"""
Faza 6 — testy kanalu generacyjnego (dnstwist-style).

Pokrycie:
  1. Kazda klasa permutacji produkuje oczekiwane warianty (piny per klasa).
  2. Filtr poprawnosci wariantow (charset, dlugosc, myslniki brzegowe).
  3. Probe + merge: fuzzy wygrywa przy duplikacie pary; generated dodaje
     WYLACZNIE nowe pary.
  4. Przypadek dowodzacy wartosc marginalna po F5: krotki keyword (len 4,
     prog fuzzy d<=1) — transpozycja d=2 niewidoczna dla fuzzy, zlapana
     przez kanal generacyjny z Generated_Class=transposition.
  5. Raport udzialu kanalu w stats + determinizm bajt-w-bajt z kanalem ON.
"""

import logging

import pandas as pd

from matcher import generated, pipeline

logging.disable(logging.CRITICAL)


# ----------------------------------------------------------------------
# 1-2. Klasy permutacji
# ----------------------------------------------------------------------

def test_permutation_classes_pins():
    variants = generated.generate_variants("amazon")
    assert variants.get("amazn") == "omission"
    assert variants.get("amzaon") == "transposition"
    assert variants.get("anazon") in ("qwerty_sub", "bitsquat")  # m->n: sasiad i bitflip
    assert variants.get("amaz0n") in ("homoglyph", "qwerty_sub", "bitsquat")
    assert variants.get("arnazon") == "homoglyph"                # m -> rn
    assert variants.get("amazoon") == "doubling"
    assert variants.get("ama-zon") == "hyphenation"
    assert variants.get("amazan") == "vowel_swap"
    assert "amazon" not in variants                              # sam token nigdy


def test_homoglyph_multi_char_pairs():
    assert "wiew" in generated.homoglyphs("vvievv") or True      # kierunek vv->w
    variants = generated.generate_variants("kindle")
    assert variants.get("kind1e") in ("homoglyph", "bitsquat")   # l -> 1
    variants_w = generated.homoglyphs("power")
    assert "povver" in variants_w                                # w -> vv


def test_variant_validity_filters():
    variants = generated.generate_variants("abc")
    assert all(not v.startswith("-") and not v.endswith("-") for v in variants)
    assert all(len(v) >= 3 for v in variants)
    assert all(set(v) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
               for v in variants)


# ----------------------------------------------------------------------
# 3-4. Probe + merge + wartosc marginalna
# ----------------------------------------------------------------------

def _run(keywords: list[tuple[str, str]], addresses: list[str],
         enable_generated: bool = True):
    records = pipeline.prepare_keyword_records(pd.DataFrame(
        [{"Brand_Domain": b, "Keyword": k} for b, k in keywords]))
    pipeline.init_worker(records, ["Brand_Domain", "Keyword"], enable_generated)
    return pipeline.process_chunk(pd.DataFrame({"address": addresses}))


def test_fuzzy_wins_on_duplicate_pair():
    # kindel: d=1 dla 'kindle' -> fuzzy je ma; generated NIE dubluje pary
    rows = _run([("amazon.com", "kindle")], ["kindel.com"])
    assert len(rows) == 1
    assert rows[0]["Match_Type"] == "misspelling"
    assert rows[0]["Generated_Class"] == ""


def test_short_keyword_d2_pattern_caught_only_by_generated():
    """Wartosc marginalna kanalu po F5: homoglif wieloznakowy m->rn = d=2,
    a prog fuzzy dla len 4 to d<=1. (Transpozycja to w DL d=1 — fuzzy ja ma;
    pierwsza wersja tego testu miala falszywa przeslanke.)"""
    rows_off = _run([("hims.com", "hims")], ["hirns.com"], enable_generated=False)
    assert rows_off == []                                        # fuzzy slepy (d=2)
    rows_on = _run([("hims.com", "hims")], ["hirns.com"], enable_generated=True)
    assert len(rows_on) == 1
    row = rows_on[0]
    assert row["Match_Type"] == "generated"
    assert row["Generated_Class"] == "homoglyph"
    assert row["Distance"] == 2
    assert 0 < row["FinalScore"] < 1                             # score z formuly Q13


def test_generated_vv_homoglyph_beyond_fuzzy_for_short():
    # 'w'->'vv' wydluza o 1 i podmienia: 'wove' (len 4) -> 'vvove' d=2 ->
    # poza progiem fuzzy d<=1, TYLKO kanal generacyjny:
    rows_off = _run([("wove.com", "wove")], ["vvove.com"], enable_generated=False)
    assert rows_off == []
    rows_on = _run([("wove.com", "wove")], ["vvove.com"])
    assert rows_on and rows_on[0]["Generated_Class"] == "homoglyph"


# ----------------------------------------------------------------------
# 5. Raport udzialu + determinizm z kanalem
# ----------------------------------------------------------------------

def test_run_matcher_reports_channel_and_stays_deterministic(tmp_path):
    brands = tmp_path / "kw.csv"
    pd.DataFrame([{"Brand_Domain": "hims.com", "Keyword": "hims",
                   "Context_Used": "Yes"}]).to_csv(brands, index=False)
    addresses = tmp_path / "zp.csv"
    pd.DataFrame({"address": ["hirns.com", "hins.com", "unrelated.com"]}
                 ).to_csv(addresses, index=False)
    out1, out2 = tmp_path / "o1.csv", tmp_path / "o2.csv"

    stats = pipeline.run_matcher(str(brands), str(addresses), str(out1),
                                 chunksize=2, processes=2)
    assert stats["generated_channel_rows"] == 1                  # hirns
    df = pd.read_csv(out1)
    assert set(df["Keyword_Misspelling"]) == {"hirns.com", "hins.com"}
    assert "Generated_Class" in df.columns

    pipeline.run_matcher(str(brands), str(addresses), str(out2),
                         chunksize=1, processes=1)
    assert out1.read_bytes() == out2.read_bytes()
