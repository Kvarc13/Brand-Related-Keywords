"""
Faza 9 — testy instrumentu ewaluacyjnego (per-vertical, gate, sampling).
"""

import csv
import importlib
import subprocess
import sys
from pathlib import Path

eval_score = importlib.import_module("tools.eval_score")
eval_build = importlib.import_module("tools.eval_build")


def _write_eval(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Brand", "Keyword", "Context_Used",
                                               "source", "label", "label_source",
                                               "notes"])
        writer.writeheader()
        writer.writerows(rows)


def _write_keywords(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Brand_Domain", "Keyword",
                                               "Context_Used", "Vertical"])
        writer.writeheader()
        writer.writerows(rows)


# ----------------------------------------------------------------------
# 1. per-vertical w score()
# ----------------------------------------------------------------------

def test_score_per_vertical_breakdown():
    labeled = {("a.com", "kindle"): "brand_direct",
               ("a.com", "books"): "reject",
               ("b.com", "hotels"): "category_ok"}
    generated = {
        ("a.com", "kindle"): {"Brand": "a.com", "Keyword": "kindle",
                              "Context_Used": "Yes", "Vertical": "e-commerce"},
        ("a.com", "books"): {"Brand": "a.com", "Keyword": "books",
                             "Context_Used": "Yes", "Vertical": "e-commerce"},
        ("b.com", "hotels"): {"Brand": "b.com", "Keyword": "hotels",
                              "Context_Used": "Yes", "Vertical": "travel"},
    }
    report = eval_score.score(labeled, generated)
    pv = report["per_vertical"]
    assert pv["e-commerce"] == {"n": 2, "precision": 0.5,
                                "share_brand_direct": 0.5}
    assert pv["travel"] == {"n": 1, "precision": 1.0, "share_brand_direct": 0.0}


# ----------------------------------------------------------------------
# 2. gate: warn = exit 0, fail = exit 1 (subprocess na prawdziwym CLI)
# ----------------------------------------------------------------------

def _run_cli(tmp_path: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    eval_path = tmp_path / "eval.csv"
    keywords_path = tmp_path / "kw.csv"
    _write_eval(eval_path, [
        {"Brand": "a.com", "Keyword": "kindle", "Context_Used": "Yes",
         "source": "x", "label": "brand_direct", "label_source": "h", "notes": ""},
        {"Brand": "a.com", "Keyword": "junk", "Context_Used": "Yes",
         "source": "x", "label": "reject", "label_source": "h", "notes": ""},
    ])
    _write_keywords(keywords_path, [
        {"Brand_Domain": "a.com", "Keyword": "kindle", "Context_Used": "Yes",
         "Vertical": "e-commerce"},
        {"Brand_Domain": "a.com", "Keyword": "junk", "Context_Used": "Yes",
         "Vertical": "e-commerce"},
    ])
    return subprocess.run(
        [sys.executable, "tools/eval_score.py", "--eval", str(eval_path),
         "--keywords", str(keywords_path)] + extra_args,
        capture_output=True, text=True)


def test_gate_warn_exits_zero_fail_exits_one(tmp_path):
    # precision = 0.5 (1 z 2)
    warn = _run_cli(tmp_path, ["--gate", "0.9", "--gate-mode", "warn"])
    assert warn.returncode == 0 and "GATE WARN" in warn.stdout
    fail = _run_cli(tmp_path, ["--gate", "0.9", "--gate-mode", "fail"])
    assert fail.returncode == 1 and "GATE FAIL" in fail.stdout
    ok = _run_cli(tmp_path, ["--gate", "0.4", "--per-vertical"])
    assert ok.returncode == 0 and "GATE OK" in ok.stdout
    assert "Per Vertical" in ok.stdout and "e-commerce" in ok.stdout


# ----------------------------------------------------------------------
# 3. sampling per-vertical (deterministyczny)
# ----------------------------------------------------------------------

def test_sample_brands_per_vertical_deterministic():
    rows = ([{"Brand_Domain": f"shop{i}.com", "Keyword": "x",
              "Vertical": "e-commerce"} for i in range(10)]
            + [{"Brand_Domain": f"air{i}.com", "Keyword": "x",
                "Vertical": "travel"} for i in range(3)])
    first = eval_build.sample_brands_per_vertical(rows, 2)
    second = eval_build.sample_brands_per_vertical(rows, 2)
    assert first == second                                    # seed=42
    assert len([b for b in first if b.startswith("shop")]) == 2
    assert len([b for b in first if b.startswith("air")]) == 2


def test_update_with_sampling_adds_only_sampled(tmp_path, monkeypatch):
    eval_path = tmp_path / "eval.csv"
    _write_eval(eval_path, [
        {"Brand": "old.com", "Keyword": "kept", "Context_Used": "Yes",
         "source": "x", "label": "brand_direct", "label_source": "h", "notes": ""},
    ])
    keywords_path = tmp_path / "kw.csv"
    rows = ([{"Brand_Domain": f"shop{i}.com", "Keyword": f"kw{i}",
              "Context_Used": "Yes", "Vertical": "e-commerce"} for i in range(6)])
    _write_keywords(keywords_path, rows)

    result = eval_build.update_eval(keywords_path, eval_path,
                                    sample_per_vertical=2)
    assert result == 0
    with open(eval_path, encoding="utf-8") as f:
        out_rows = list(csv.DictReader(f))
    assert out_rows[0]["Brand"] == "old.com"                  # etykieta nietknieta
    added_brands = {r["Brand"] for r in out_rows[1:]}
    assert len(added_brands) == 2                             # tylko probka


# ----------------------------------------------------------------------
# 4. run_pipeline (czysty runner) + batch: konsumpcja stanu na expired
# ----------------------------------------------------------------------

def test_run_pipeline_stops_on_failing_step(tmp_path):
    """Runner z fake-krokiem: pad zatrzymuje sekwencje z kodem kroku."""
    from tools import golden_run, run_pipeline  # noqa: F401
    ok = tmp_path / "ok.py"; ok.write_text("print('ok')", encoding="utf-8")
    bad = tmp_path / "bad.py"; bad.write_text("import sys; sys.exit(7)",
                                              encoding="utf-8")
    spec_ok = golden_run.StepSpec(key="x", script="ok.py")
    spec_bad = golden_run.StepSpec(key="y", script="bad.py")
    assert golden_run.run_step(spec_ok, tmp_path, sys.executable)["returncode"] == 0
    assert golden_run.run_step(spec_bad, tmp_path, sys.executable)["returncode"] == 7


def test_batch_terminal_status_consumes_state(tmp_path, monkeypatch):
    """expired/failed nie moze zostawic batch_state.json (wieczny trup)."""
    import types
    from keywordgen import batch as batch_mode
    from keywordgen.llm import TransportError
    import pytest as _pytest

    state_path = tmp_path / "batch_state.json"
    state_path.write_text('{"batch_id": "batch_dead"}', encoding="utf-8")
    (tmp_path / "ctx.json").write_text("[]", encoding="utf-8")

    class FakeBatches:
        def retrieve(self, batch_id):
            return types.SimpleNamespace(status="expired", request_counts=None)

    class FakeClient:
        batches = FakeBatches()

    monkeypatch.setattr("keywordgen.batch.OpenAI", lambda: FakeClient(),
                        raising=False)
    # run_batch importuje OpenAI lokalnie — podmien w module openai:
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda: FakeClient())

    config = {"batch_state_file": str(state_path),
              "context_json": str(tmp_path / "ctx.json")}
    with _pytest.raises(TransportError, match="expired"):
        batch_mode.run_batch(["a.com"], config, transport=None)
    assert not state_path.exists()          # stan skonsumowany — brak petli trupa


def test_run_pipeline_preflight_respects_sequence(tmp_path):
    """Wejscie produkowane przez wczesniejszy krok sekwencji NIE blokuje startu;
    testowana jest sama logika preflightu (pelny main odpalilby realny Step 2)."""
    # Brands.csv istnieje (zewnetrzne wejscie 2); Transformed_Keywords.csv NIE —
    # ale produkuje je Step 2, wiec nie moze blokowac Step 3.
    (tmp_path / "Brands.csv").write_text("Brand\nx.com\n", encoding="utf-8")
    (tmp_path / "Zeropark_Domain_List.csv").write_text("address\n", encoding="utf-8")
    from tools.golden_run import STEPS, preflight
    produced: set[str] = set()
    hard: list[str] = []
    for key in ("2", "3"):
        errors, _ = preflight(STEPS[key], tmp_path)
        for error in errors:
            name = error.split(":")[-1].strip()
            if name not in produced:
                hard.append(name)
        produced.update(getattr(STEPS[key], "required_outputs", ()) or ())
        produced.update(getattr(STEPS[key], "optional_outputs", ()) or ())
    assert "Transformed_Keywords.csv" not in hard      # producent w sekwencji
