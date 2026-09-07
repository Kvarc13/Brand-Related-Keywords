"""
Faza 0 — testy ksztaltu na dostarczonych sample'ach.

Pinuja kontrakty, ktorych zaden pozniejszy refaktor nie moze zlamac
NIESWIADOMIE (swiadome zmiany = aktualizacja fixtures + tych testow):

  1. Wymagane kolumny obu plikow (misspellings sa nadzbiorem — realny plik
     niesie dodatkowe kolumny z listy Zeropark, wiec test to podzbior).
  2. Zakresy wartosci metryk.
  3. Inwariant klasy exact-SLD: Score_Custom == 1.00  <=>  Distance == 0
     (fundament pod przyszly Match_Type=exact_other_tld w Fazie 5).
  4. Kontrakt sortowania raportu klienckiego: Brand rosnaco,
     w obrebie brandu Total Visits nierosnaco (obecne zachowanie Step 5).
"""

import csv
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "samples"
MISSPELLINGS = SAMPLES_DIR / "Keyword_Specific_Misspellings.sample.csv"
CLIENT_REPORT = SAMPLES_DIR / "Brandable_Domains.sample.csv"

MISSPELLINGS_REQUIRED_COLUMNS = {
    "Brand",
    "Keyword",
    "Context_Used",
    "Keyword_Misspelling",
    "FinalScore",
    "Score_Custom",
    "Distance",
    "Neighbors",
    "Score_NormDamerauLevenshtein",
    "Score_JaroWinkler",
    "Score_Dice",
    "Phonetic_Match",
}

CLIENT_REQUIRED_COLUMNS = {
    "Brand",
    "Keyword",
    "Target Hash",
    "Total Visits",
    "Avg. Cost",
    "Similarity_Score",
}

UNIT_INTERVAL_COLUMNS = (
    "FinalScore",
    "Score_Custom",
    "Score_NormDamerauLevenshtein",
    "Score_JaroWinkler",
    "Score_Dice",
)


def load_rows(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows, f"pusty sample: {path.name}"
    return rows


# ----------------------------------------------------------------------
# Misspellings (krok posredni)
# ----------------------------------------------------------------------

def test_misspellings_required_columns_present():
    rows = load_rows(MISSPELLINGS)
    missing = MISSPELLINGS_REQUIRED_COLUMNS - set(rows[0].keys())
    assert not missing, f"brak wymaganych kolumn: {sorted(missing)}"


def test_misspellings_value_ranges():
    for row in load_rows(MISSPELLINGS):
        for column in UNIT_INTERVAL_COLUMNS:
            value = float(row[column])
            assert 0.0 <= value <= 1.0, f"{column}={value} poza [0,1]: {row}"
        assert int(row["Distance"]) >= 0
        assert int(row["Neighbors"]) >= 0
        assert row["Phonetic_Match"] in {"True", "False"}
        assert row["Context_Used"] in {"Yes", "No (Fallback)"}
        assert row["Brand"] and row["Keyword"] and row["Keyword_Misspelling"]


def test_misspellings_exact_sld_invariant():
    """Score_Custom == 1.00 dokladnie wtedy, gdy Distance == 0.

    To wlasnosc obecnej formuly (score = 1 - dist/len) i jednoczesnie
    definicja klasy 'ten sam SLD, inny TLD' uzywanej w planie Fazy 5/8.
    """
    for row in load_rows(MISSPELLINGS):
        is_exact = abs(float(row["Score_Custom"]) - 1.0) < 1e-9
        is_zero_distance = int(row["Distance"]) == 0
        assert is_exact == is_zero_distance, row


# ----------------------------------------------------------------------
# Raport kliencki
# ----------------------------------------------------------------------

def test_client_report_required_columns_present():
    rows = load_rows(CLIENT_REPORT)
    missing = CLIENT_REQUIRED_COLUMNS - set(rows[0].keys())
    assert not missing, f"brak wymaganych kolumn: {sorted(missing)}"


def test_client_report_value_ranges():
    for row in load_rows(CLIENT_REPORT):
        assert 0.0 <= float(row["Similarity_Score"]) <= 1.0, row
        assert int(row["Total Visits"]) >= 0, row
        assert float(row["Avg. Cost"]) >= 0.0, row
        assert row["Brand"] and row["Keyword"] and row["Target Hash"]


def test_client_report_sorted_brand_asc_visits_desc():
    """Obecny kontrakt Step 5: sort_values([Brand, Total Visits], [asc, desc])."""
    previous_brand = ""
    previous_visits = None
    for row in load_rows(CLIENT_REPORT):
        brand = row["Brand"]
        visits = int(row["Total Visits"])
        if brand != previous_brand:
            assert brand > previous_brand, f"Brand nie rosnie: {previous_brand!r} -> {brand!r}"
            previous_brand = brand
        else:
            assert visits <= previous_visits, (
                f"Total Visits rosnie w obrebie {brand}: {previous_visits} -> {visits}"
            )
        previous_visits = visits
