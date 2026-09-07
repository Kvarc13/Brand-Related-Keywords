"""
Faza 8 — testy warstwy polityk i eksportow (offline).
"""

import logging
from pathlib import Path

import pandas as pd
import pytest

from keywordgen.llm import FakeTransport
from keywordgen.models import JudgeResult, JudgeVerdict
from report import pipeline, policies
from report.llm_gate import build_gate

logging.disable(logging.CRITICAL)


def _row(**overrides):
    base = {"Brand": "amazon.com", "Keyword": "kindle", "Specificity": "branded",
            "Keyword_Misspelling": "kindel.com", "Match_Type": "misspelling",
            "FinalScore": 0.9, "Distance": 1, "Score_JaroWinkler": 0.95,
            "TLD_Match": True, "In_Context": "Yes", "Context_Used": "Yes"}
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# 1. Macierz polityk Q11 (cut_reason)
# ----------------------------------------------------------------------

def _merged_row(**overrides):
    row = _row(**{"Total Visits": 50})
    row.update(overrides)
    return row


def test_policy_matrix_q11():
    tranco = {"books.com"}
    assert policies.cut_reason(_merged_row(), tranco, 0) == ""
    assert policies.cut_reason(_merged_row(Match_Type="affix"), tranco, 0) == ""
    assert policies.cut_reason(_merged_row(Match_Type="generated"), tranco, 0) == ""
    # exact_other_tld: brand tier widoczny, kategoria cieta
    assert policies.cut_reason(_merged_row(Match_Type="exact_other_tld",
                                           Specificity="brand_core"), tranco, 0) == ""
    assert policies.cut_reason(
        _merged_row(Match_Type="exact_other_tld", Specificity="category_strong"),
        tranco, 0) == "exact_other_tld_non_brand_tier"
    # self zawsze poza klientem
    assert policies.cut_reason(_merged_row(Match_Type="self"), tranco, 0) == "self_domain"
    # popularna domena cieta
    assert policies.cut_reason(
        _merged_row(Keyword_Misspelling="books.com"), tranco, 0) == "popular_domain"
    # prog ruchu
    assert policies.cut_reason(_merged_row(**{"Total Visits": 3}), tranco,
                               min_visits=10) == "below_min_visits"


def test_tranco_loader_graceful_absence(tmp_path):
    assert policies.load_tranco(tmp_path / "missing.csv") == set()
    tranco_file = tmp_path / "tranco.csv"
    tranco_file.write_text("1,google.com\n2,books.com\n", encoding="utf-8")
    assert policies.load_tranco(tranco_file) == {"google.com", "books.com"}


# ----------------------------------------------------------------------
# 2. Dedup per-brand (Q10) + join
# ----------------------------------------------------------------------

def test_per_brand_dedup_not_global():
    misspellings = pd.DataFrame([
        _row(Keyword="kindle", FinalScore=0.9),
        _row(Keyword="alexa", FinalScore=0.95),                  # ten sam brand+adres
        _row(Brand="kindleco.com", Keyword="kindle", FinalScore=0.7),  # INNY brand
    ])
    targetdata = pd.DataFrame([{
        "Target Address": "kindel.com", "Target Hash": "th1",
        "Total Visits": 10, "Avg. Cost": 0.1, "Publisher Feed Hash": "pf1"}])
    merged = pipeline.join_and_dedup(misspellings, targetdata)
    amazon_rows = merged[merged["Brand"] == "amazon.com"]
    assert len(amazon_rows) == 1
    assert amazon_rows.iloc[0]["Keyword"] == "alexa"             # kanonicznie najlepszy
    assert len(merged[merged["Brand"] == "kindleco.com"]) == 1   # per-brand, NIE global
    assert (merged["Similarity_Score"] == merged["FinalScore"]).all()


# ----------------------------------------------------------------------
# 3. Walidacja kolumn (glosna)
# ----------------------------------------------------------------------

def test_loud_column_validation():
    with pytest.raises(SystemExit, match="bez kolumn"):
        pipeline.validate_columns(pd.DataFrame({"Brand": []}),
                                  pipeline.MISSPELLING_REQUIRED, "x.csv")


# ----------------------------------------------------------------------
# 4. E2E: trzy eksporty, audyt Cut_Reason, grouped, sort tier->visits
# ----------------------------------------------------------------------

def _write_e2e_inputs(tmp_path: Path):
    misspellings = tmp_path / "missp.csv"
    pd.DataFrame([
        _row(),                                                   # widoczny
        _row(Keyword="prime", Specificity="category_strong",
             Keyword_Misspelling="prime.app", Match_Type="exact_other_tld",
             FinalScore=0.9, Distance=0),                         # ciety
        _row(Keyword="amazon", Specificity="brand_core",
             Keyword_Misspelling="amazon.com", Match_Type="self",
             FinalScore=0.9, Distance=0),                         # ciety
        _row(Keyword="amazon", Specificity="brand_core",
             Keyword_Misspelling="amazon.pl", Match_Type="exact_other_tld",
             FinalScore=0.9, Distance=0),                         # widoczny (tier!)
    ]).to_csv(misspellings, index=False)
    targetdata = tmp_path / "td.csv"
    pd.DataFrame([
        {"Target Address": "kindel.com", "Target Hash": "t1",
         "Total Visits": 50, "Avg. Cost": 0.1, "Publisher Feed Hash": "p1"},
        {"Target Address": "prime.app", "Target Hash": "t2",
         "Total Visits": 900, "Avg. Cost": 0.2, "Publisher Feed Hash": "p2"},
        {"Target Address": "amazon.com", "Target Hash": "t3",
         "Total Visits": 5, "Avg. Cost": 0.0, "Publisher Feed Hash": "p3"},
        {"Target Address": "amazon.pl", "Target Hash": "t4",
         "Total Visits": 200, "Avg. Cost": 0.3, "Publisher Feed Hash": "p4"},
    ]).to_csv(targetdata, index=False)
    return misspellings, targetdata


def test_run_report_e2e(tmp_path):
    misspellings, targetdata = _write_e2e_inputs(tmp_path)
    config = {"min_visits": 0, "tranco_file": str(tmp_path / "no_tranco.csv"),
              "output_client": str(tmp_path / "client.csv"),
              "output_internal": str(tmp_path / "internal.csv"),
              "output_grouped": str(tmp_path / "grouped.md")}
    stats = pipeline.run_report(str(misspellings), str(targetdata), config)
    assert stats["joined"] == 4 and stats["client_rows"] == 2
    assert stats["cut"] == {"exact_other_tld_non_brand_tier": 1, "self_domain": 1}

    client = pd.read_csv(config["output_client"])
    assert set(client["Keyword_Misspelling"]) == {"kindel.com", "amazon.pl"}
    assert "Cut_Reason" not in client.columns
    assert "Publisher Feed Hash" not in client.columns            # feed tylko internal
    # sort tier -> visits: brand_core (amazon.pl) przed branded (kindel)
    assert list(client["Keyword_Misspelling"]) == ["amazon.pl", "kindel.com"]

    internal = pd.read_csv(config["output_internal"])
    assert len(internal) == 4                                     # PELNY audyt
    reasons = dict(zip(internal["Keyword_Misspelling"], internal["Cut_Reason"]
                       .fillna("")))
    assert reasons["prime.app"] == "exact_other_tld_non_brand_tier"
    assert reasons["amazon.com"] == "self_domain"

    grouped = Path(config["output_grouped"]).read_text(encoding="utf-8")
    assert "## amazon.com" in grouped
    assert "### kindle  `branded`" in grouped
    assert "kindel.com" in grouped and "prime.app" not in grouped # ciete poza widokiem


# ----------------------------------------------------------------------
# 5. Bramka LLM (flaga; FakeTransport)
# ----------------------------------------------------------------------

def test_llm_gate_rejects_low_score_rows(tmp_path):
    misspellings, targetdata = _write_e2e_inputs(tmp_path)
    fake = FakeTransport(judge_results=[JudgeResult(verdicts=[
        JudgeVerdict(keyword="kindel.com", accept=False)])])
    config = {"min_visits": 0, "tranco_file": str(tmp_path / "no.csv"),
              "enable_llm_gate": True,
              "output_client": str(tmp_path / "c.csv"),
              "output_internal": str(tmp_path / "i.csv"),
              "output_grouped": str(tmp_path / "g.md")}
    stats = pipeline.run_report(str(misspellings), str(targetdata), config,
                                llm_gate=build_gate(fake, threshold=0.95))
    # bramka tnie TYLKO jawne odrzucenia (fail-open dla braku werdyktu):
    # kindel.com odrzucony skryptem, amazon.pl bez werdyktu -> zostaje
    assert stats["cut"].get("llm_gate") == 1
    assert stats["client_rows"] == 1
    client = pd.read_csv(config["output_client"])
    assert list(client["Keyword_Misspelling"]) == ["amazon.pl"]


def test_llm_gate_error_keeps_rows(tmp_path):
    misspellings, targetdata = _write_e2e_inputs(tmp_path)
    config = {"min_visits": 0, "tranco_file": str(tmp_path / "no.csv"),
              "enable_llm_gate": True,
              "output_client": str(tmp_path / "c.csv"),
              "output_internal": str(tmp_path / "i.csv"),
              "output_grouped": str(tmp_path / "g.md")}
    stats = pipeline.run_report(str(misspellings), str(targetdata), config,
                                llm_gate=build_gate(FakeTransport(), 0.95))
    assert "llm_gate" not in stats["cut"]                         # blad = zostaja
    assert stats["client_rows"] == 2
