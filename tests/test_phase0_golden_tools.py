"""
Faza 0 — testy jednostkowe narzedzi golden.

Zakres:
  - golden_run: rejestr krokow kompletny wzgledem plikow w repo, kolejnosc
    kanoniczna, liczenie wierszy, hash z capem, wykrywanie klucza w .env,
    preflight (fail-loud na brakujacych wejsciach/kluczu API).
  - golden_diff: identycznosc, dodane/usuniete wiersze (multizbior),
    zmiana kolejnosci kolumn, zmiana schematu, brakujacy plik,
    JSON per domena+status, zrzuty .added/.removed.

Celowo NIE uruchamiamy tu prawdziwych krokow (wymagaja zewnetrznych danych
i uslug) — golden run wykonywany jest po Waszej stronie na realnym wejsciu.
"""

import json
from pathlib import Path

import pytest

from tools import golden_diff, golden_run

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# golden_run
# ----------------------------------------------------------------------

def test_step_registry_scripts_exist_in_repo():
    """Kazdy zadeklarowany krok musi istniec obok tools/ — test pakietu."""
    for spec in golden_run.STEPS.values():
        assert (REPO_ROOT / spec.script).exists(), f"brak skryptu: {spec.script}"


def test_step_registry_covers_canonical_order():
    assert set(golden_run.STEPS) == set(golden_run.CANONICAL_ORDER)


def test_order_steps_sorts_canonically_and_dedupes():
    assert golden_run.order_steps(["5", "3", "4.5", "3"]) == ["3", "4.5", "5"]


def test_order_steps_rejects_unknown():
    with pytest.raises(ValueError):
        golden_run.order_steps(["2", "7"])


def test_count_rows_csv_and_json(tmp_path):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    assert golden_run.count_rows(csv_path) == 3

    json_path = tmp_path / "data.json"
    json_path.write_text(json.dumps([{"domain": "a"}, {"domain": "b"}]), encoding="utf-8")
    assert golden_run.count_rows(json_path) == 2

    other = tmp_path / "notes.log"
    other.write_text("x", encoding="utf-8")
    assert golden_run.count_rows(other) is None


def test_sha256_cap_skips_large_files(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"0123456789")
    assert golden_run.sha256_file(path, cap_bytes=5).startswith("skipped:")
    assert len(golden_run.sha256_file(path)) == 64  # pelny hex bez capa


def test_env_has_key_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert golden_run.env_has_key(tmp_path, "OPENAI_API_KEY") is False

    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    assert golden_run.env_has_key(tmp_path, "OPENAI_API_KEY") is True

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert golden_run.env_has_key(tmp_path / "nieistniejacy", "OPENAI_API_KEY") is True


def test_preflight_reports_all_missing_inputs(tmp_path):
    errors, warnings = golden_run.preflight(golden_run.STEPS["3"], tmp_path)
    assert len(errors) == 2  # oba wejscia kroku 3 brakuja
    assert any("Transformed_Keywords.csv" in e for e in errors)
    assert any("Zeropark_Domain_List.csv" in e for e in errors)
    assert warnings == []


def test_preflight_step2_fails_loud_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "Brands.csv").write_text("Brand\nexample.com\n", encoding="utf-8")
    errors, warnings = golden_run.preflight(golden_run.STEPS["2"], tmp_path)
    assert any("OPENAI_API_KEY" in e for e in errors)
    # brand_content_final.json to wejscie opcjonalne -> ostrzezenie, nie blad
    assert any("brand_content_final.json" in w for w in warnings)


# ----------------------------------------------------------------------
# golden_diff — pomocnicze buildery
# ----------------------------------------------------------------------

BASE_CSV = "Brand,Keyword\namazon.com,Kindle\namazon.com,Prime\nbooking.com,hotels\n"
BASE_JSON = [
    {"domain": "amazon.com", "status": "success"},
    {"domain": "booking.com", "status": "success"},
]


def make_dir(root: Path, name: str, csv_text: str, json_data) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "out.csv").write_text(csv_text, encoding="utf-8")
    (directory / "content.json").write_text(json.dumps(json_data), encoding="utf-8")
    return directory


# ----------------------------------------------------------------------
# golden_diff — przypadki
# ----------------------------------------------------------------------

def test_diff_identical_dirs(tmp_path):
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", BASE_CSV, BASE_JSON)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    assert report["identical"] is True
    assert all(f["status"] == "identical" for f in report["files"].values())


def test_diff_detects_added_and_removed_rows(tmp_path):
    changed = "Brand,Keyword\namazon.com,Kindle\namazon.com,Alexa\nbooking.com,hotels\n"
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", changed, BASE_JSON)
    out = tmp_path / "out"
    report = golden_diff.compare_dirs(baseline, candidate, out)

    file_report = report["files"]["out.csv"]
    assert report["identical"] is False
    assert file_report["status"] == "differs"
    assert file_report["rows_added"] == 1
    assert file_report["rows_removed"] == 1
    assert ["amazon.com", "Alexa"] in file_report["sample_added"]
    assert ["amazon.com", "Prime"] in file_report["sample_removed"]
    assert (out / "out.csv.added.csv").exists()
    assert (out / "out.csv.removed.csv").exists()


def test_diff_multiset_counts_duplicates(tmp_path):
    """Zduplikowany wiersz to roznica — Counter, nie set."""
    duplicated = BASE_CSV + "amazon.com,Kindle\n"
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", duplicated, BASE_JSON)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    file_report = report["files"]["out.csv"]
    assert file_report["rows_added"] == 1
    assert file_report["rows_removed"] == 0


def test_diff_header_reorder_is_flagged_but_rows_realigned(tmp_path):
    reordered = "Keyword,Brand\nKindle,amazon.com\nPrime,amazon.com\nhotels,booking.com\n"
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", reordered, BASE_JSON)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    file_report = report["files"]["out.csv"]
    assert file_report["status"] == "differs"          # verbatim: kolejnosc tez zamrozona
    assert file_report["header_order_changed"] is True
    assert file_report["rows_added"] == 0              # ale tresc wierszy zgodna
    assert file_report["rows_removed"] == 0


def test_diff_schema_change_reported_without_row_noise(tmp_path):
    extra_column = "Brand,Keyword,Extra\namazon.com,Kindle,x\n"
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", extra_column, BASE_JSON)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    file_report = report["files"]["out.csv"]
    assert file_report["status"] == "schema_differs"
    assert file_report["columns_extra_in_candidate"] == ["Extra"]
    assert "rows_added" not in file_report


def test_diff_missing_candidate_file(tmp_path):
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "content.json").write_text(json.dumps(BASE_JSON), encoding="utf-8")
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    assert report["files"]["out.csv"]["status"] == "missing_in_candidate"
    assert report["identical"] is False


def test_diff_json_domain_level_report(tmp_path):
    changed_json = [
        {"domain": "amazon.com", "status": "error"},      # zmiana statusu
        {"domain": "adidas.com", "status": "success"},    # nowa domena
    ]                                                     # booking.com usuniety
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    candidate = make_dir(tmp_path, "candidate", BASE_CSV, changed_json)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    file_report = report["files"]["content.json"]
    assert file_report["status"] == "differs"
    assert file_report["domains_added"] == 1
    assert file_report["domains_removed"] == 1
    assert file_report["status_changed"] == 1
    assert ("amazon.com", "success", "error") in [
        tuple(item) for item in file_report["sample_status_changed"]
    ]


def test_diff_skips_metrics_json(tmp_path):
    baseline = make_dir(tmp_path, "baseline", BASE_CSV, BASE_JSON)
    (baseline / "metrics.json").write_text("{}", encoding="utf-8")
    candidate = make_dir(tmp_path, "candidate", BASE_CSV, BASE_JSON)
    report = golden_diff.compare_dirs(baseline, candidate, tmp_path / "out")
    assert "metrics.json" not in report["files"]
    assert report["identical"] is True
