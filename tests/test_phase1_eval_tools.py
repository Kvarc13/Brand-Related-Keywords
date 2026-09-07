"""
Faza 1 — testy jednostkowe narzedzi eval.

Zakres:
  - eval_build: integralnosc kotwic planu, wstrzykniecie SLD z etykieta
    definicyjna, dedup case-insensitive z kumulacja zrodel, priorytet
    pierwszej etykiety, sortowanie (nieoznaczone najpierw), ochrona
    istniejacego pliku (--force).
  - eval_score: matematyka precision/coverage, rozbicie per Context_Used,
    wiersze eval nieaktywne, wykrywanie pustych i blednych etykiet.
"""

import csv
from pathlib import Path

from tools import eval_build, eval_score


# ----------------------------------------------------------------------
# Pomocnicze
# ----------------------------------------------------------------------

def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def keywords_file(tmp_path: Path, rows: list[list[str]]) -> Path:
    return write_csv(tmp_path / "Transformed_Keywords.csv",
                     ["Brand_Domain", "Keyword", "Context_Used"], rows)


def brands_file(tmp_path: Path, brands: list[str]) -> Path:
    return write_csv(tmp_path / "Brands.csv", ["Brand"], [[b] for b in brands])


# ----------------------------------------------------------------------
# eval_build — kotwice i budowa
# ----------------------------------------------------------------------

def test_plan_anchors_integrity():
    """Kotwice zgodne z decyzjami planu: poprawne etykiety, graniczne puste."""
    valid = set(eval_build.LABELS) | {""}
    boundary = {("americanexpress.com", "rewards"), ("aroma360.com", "ambient")}
    for brand, keyword, label in eval_build.PLAN_ANCHORS:
        assert label in valid, (brand, keyword, label)
        if (brand, keyword) in boundary:
            assert label == "", f"graniczna kotwica {keyword} nie moze miec etykiety"


def test_sld_of_first_segment():
    assert eval_build.sld_of("titan.fitness") == "titan"
    assert eval_build.sld_of("Amazon.com") == "amazon"
    assert eval_build.sld_of("hims.com") == "hims"


def test_build_injects_sld_rows_with_definition_label(tmp_path):
    rows = eval_build.build_eval_rows(
        keyword_rows=[{"Brand_Domain": "hims.com", "Keyword": "Sildenafil",
                       "Context_Used": "Yes"}],
        brands=["hims.com", "gilt.com"],
    )
    by_key = {(r["Brand"].lower(), r["Keyword"].lower()): r for r in rows}

    hims_sld = by_key[("hims.com", "hims")]
    assert hims_sld["label"] == "brand_direct"
    assert hims_sld["label_source"] == "sld_definition"
    assert hims_sld["source"] == "sld_anchor"

    assert ("gilt.com", "gilt") in by_key  # SLD nawet dla brandu bez generacji
    assert by_key[("hims.com", "sildenafil")]["label"] == ""  # generacja bez etykiety


def test_build_dedup_case_insensitive_merges_sources_keeps_first_casing():
    rows = eval_build.build_eval_rows(
        keyword_rows=[{"Brand_Domain": "amazon.com", "Keyword": "KINDLE",
                       "Context_Used": "Yes"}],
        brands=["amazon.com"],
    )
    by_key = {(r["Brand"].lower(), r["Keyword"].lower()): r for r in rows}
    kindle = by_key[("amazon.com", "kindle")]
    assert kindle["Keyword"] == "KINDLE"                      # casing z generacji
    assert "baseline_run" in kindle["source"] and "plan_anchor" in kindle["source"]
    assert kindle["label"] == "brand_direct"                  # etykieta z kotwicy
    assert kindle["label_source"] == "plan_anchor"


def test_build_anchor_label_does_not_override_existing():
    """Pierwsza przypisana etykieta wygrywa (SLD przed kotwica i odwrotnie stabilnie)."""
    rows = eval_build.build_eval_rows(keyword_rows=[], brands=["amazon.com"])
    by_key = {(r["Brand"].lower(), r["Keyword"].lower()): r for r in rows}
    # 'amazon' z sld_definition; kotwice amazona (Kindle itd.) osobno
    assert by_key[("amazon.com", "amazon")]["label_source"] == "sld_definition"
    assert by_key[("amazon.com", "kindle")]["label_source"] == "plan_anchor"


def test_build_sorts_unlabeled_first_within_brand():
    rows = eval_build.build_eval_rows(
        keyword_rows=[
            {"Brand_Domain": "amazon.com", "Keyword": "zzz", "Context_Used": "Yes"},
            {"Brand_Domain": "amazon.com", "Keyword": "aaa", "Context_Used": "Yes"},
        ],
        brands=["amazon.com"],
    )
    amazon = [r for r in rows if r["Brand"] == "amazon.com"]
    labels = [bool(r["label"]) for r in amazon]
    assert labels == sorted(labels), "nieoznaczone musza poprzedzac oznaczone"
    unlabeled = [r["Keyword"] for r in amazon if not r["label"]]
    assert unlabeled == sorted(unlabeled, key=str.lower)


def test_build_cli_refuses_overwrite_without_force(tmp_path, monkeypatch, capsys):
    keywords = keywords_file(tmp_path, [["amazon.com", "Kindle", "Yes"]])
    brands = brands_file(tmp_path, ["amazon.com"])
    out = tmp_path / "eval.csv"
    out.write_text("Brand,Keyword,label\nx,y,brand_direct\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "eval_build.py", "--keywords", str(keywords), "--brands", str(brands),
        "--out", str(out),
    ])
    assert eval_build.main() == 2
    assert "UTRATE" in capsys.readouterr().out
    assert "x,y,brand_direct" in out.read_text(encoding="utf-8")  # nietkniety

    monkeypatch.setattr("sys.argv", [
        "eval_build.py", "--keywords", str(keywords), "--brands", str(brands),
        "--out", str(out), "--force",
    ])
    assert eval_build.main() == 0
    content = out.read_text(encoding="utf-8")
    assert "Kindle" in content and "x,y" not in content


# ----------------------------------------------------------------------
# eval_score — matematyka
# ----------------------------------------------------------------------

def eval_file(tmp_path: Path, rows: list[list[str]]) -> Path:
    return write_csv(tmp_path / "keyword_eval.csv",
                     ["Brand", "Keyword", "Context_Used", "source",
                      "label", "label_source", "notes"], rows)


def test_score_precision_and_coverage(tmp_path):
    labeled, blank, invalid = eval_score.read_eval(eval_file(tmp_path, [
        ["amazon.com", "Kindle", "Yes", "a", "brand_direct", "", ""],
        ["amazon.com", "Prime", "Yes", "a", "brand_direct", "", ""],
        ["booking.com", "hotels", "Yes", "a", "category_ok", "", ""],
        ["aroma360.com", "olfactory", "Yes", "a", "reject", "", ""],
        ["aroma360.com", "ambient", "Yes", "a", "", "", ""],          # pusta
        ["amazon.com", "OldRunOnly", "Yes", "a", "brand_direct", "", ""],  # nieaktywna
    ]))
    assert blank == 1 and invalid == []

    generated = eval_score.read_generated(keywords_file(tmp_path, [
        ["amazon.com", "kindle", "Yes"],            # case-insensitive match
        ["amazon.com", "Prime", "Yes"],
        ["booking.com", "hotels", "Yes"],
        ["aroma360.com", "olfactory", "No (Fallback)"],
        ["aroma360.com", "NewUnlabeled", "Yes"],
    ]))
    report = eval_score.score(labeled, generated)

    assert report["generated_pairs"] == 5
    assert report["labeled_generated"] == 4
    assert report["coverage"] == 4 / 5
    assert report["precision"] == 3 / 4                      # 2x brand_direct + 1x category_ok / 4
    assert report["share_brand_direct"] == 2 / 4
    assert report["label_counts"] == {"brand_direct": 2, "category_ok": 1, "reject": 1}
    assert report["per_context"]["No (Fallback)"] == {"reject": 1}
    assert report["eval_rows_inactive"] == 1                 # OldRunOnly
    assert report["unlabeled_generated"] == 1
    assert report["unlabeled_sample"] == ["aroma360.com: NewUnlabeled"]


def test_score_dedups_generated_pairs(tmp_path):
    labeled, _, _ = eval_score.read_eval(eval_file(tmp_path, [
        ["amazon.com", "Kindle", "Yes", "a", "brand_direct", "", ""],
    ]))
    generated = eval_score.read_generated(keywords_file(tmp_path, [
        ["amazon.com", "Kindle", "Yes"],
        ["amazon.com", "KINDLE", "Yes"],   # duplikat po case-fold
    ]))
    report = eval_score.score(labeled, generated)
    assert report["generated_pairs"] == 1
    assert report["precision"] == 1.0


def test_read_eval_flags_invalid_labels(tmp_path):
    _, _, invalid = eval_score.read_eval(eval_file(tmp_path, [
        ["amazon.com", "Kindle", "Yes", "a", "brand-direct", "", ""],  # zla wartosc
    ]))
    assert len(invalid) == 1 and "brand-direct" in invalid[0]


def test_score_empty_generation_yields_na(tmp_path):
    labeled, _, _ = eval_score.read_eval(eval_file(tmp_path, [
        ["amazon.com", "Kindle", "Yes", "a", "brand_direct", "", ""],
    ]))
    report = eval_score.score(labeled, {})
    assert report["precision"] is None
    assert report["coverage"] is None
    assert report["eval_rows_inactive"] == 1
