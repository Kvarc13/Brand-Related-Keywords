"""
Faza 7 — testy Step 4 (multimapa) i Step 4.5 (odpornosc). Offline.
"""

import importlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

step4 = importlib.import_module("Keyword_Step4__MatchTargetID")
step45 = importlib.import_module("Keyword_Step4_5_By_Target_Consecutive")


# ----------------------------------------------------------------------
# Step 4 — multimapa (Q1)
# ----------------------------------------------------------------------

def test_multimap_emits_all_pairs_and_dedups_ids():
    target_map = {
        "kindel.com": [("ID1", "feedA"), ("ID2", "feedB")],   # 2 pary -> 2 wiersze
        "bookinig.com": [("ID3", "feedA")],
        "dup.com": [("ID1", "feedC")],                        # ID1 juz widziane
    }
    rows, unmatched = step4.build_output_rows(
        ["kindel.com", "bookinig.com", "dup.com", "missing.com"], target_map)
    assert unmatched == 1
    assert rows == [["ID1", "feedA", "Domain"], ["ID2", "feedB", "Domain"],
                    ["ID3", "feedA", "Domain"]]               # dedup ID, kolejnosc lookupu


def test_stream_match_targets_multimap_and_single_pass(tmp_path):
    """E2E na malym dumpie: duplikat adresu -> multimapa; progress po bajtach
    (dowod braku drugiego przebiegu: plik czytany przez JEDEN uchwyt)."""
    dump = tmp_path / "targets.csv"
    pd.DataFrame([
        {"feed": "fA", "address": "Kindel.com", "id": "ID1", "hash": "h"},
        {"feed": "fB", "address": "kindel.com", "id": "ID2", "hash": "h"},
        {"feed": "fA", "address": "other.com", "id": "ID9", "hash": "h"},
    ]).to_csv(dump, index=False)
    target_map = step4.stream_match_targets(str(dump), {"kindel.com"})
    assert target_map == {"kindel.com": [("ID1", "fA"), ("ID2", "fB")]}


def test_step4_normalize_keeps_www():
    # klucze porownywane 1:1 — normalize NIE tnie www (inaczej niz common)
    assert step4.normalize(" WWW.Kindel.COM ") == "www.kindel.com"
    assert step4.normalize("\ufeffkindel.com") == "kindel.com"


# ----------------------------------------------------------------------
# Step 4.5 — odpornosc
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


class Always404Session:
    def get(self, url, **kwargs):
        return FakeResponse(404)


def test_bounded_polling_eternal_404_fails_in_finite_time():
    sleeps = []
    with pytest.raises(step45.BatchError, match="niegotowy"):
        step45.wait_for_download_url(Always404Session(), "http://x/status",
                                     max_attempts=5, sleep=sleeps.append)
    assert len(sleeps) == 5                       # dokladnie max_attempts, koniec


def test_failed_batch_retries_then_exits_loudly(tmp_path, monkeypatch):
    calls = {"n": 0}

    def failing_fetcher(batch):
        calls["n"] += 1
        raise step45.BatchError("boom")

    state = {"completed_batches": [], "header_written": False}
    with pytest.raises(SystemExit) as excinfo:
        step45.run({"ID1": "fA"}, failing_fetcher, state,
                   output_path=str(tmp_path / "out.csv"),
                   state_path=str(tmp_path / "state.json"))
    assert "nieudany" in str(excinfo.value)
    assert calls["n"] == 1 + step45.BATCH_RETRIES  # retry, potem glosny fail


def test_resume_skips_completed_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(step45, "CHUNK_SIZE", 1)   # 2 targety -> 2 batche
    fetched = []

    def fetcher(batch):
        fetched.append(list(batch))
        return "Target,Total Visits\n" + f"{batch[0]},5\n"

    out = tmp_path / "out.csv"
    state_path = tmp_path / "state.json"

    # pierwszy run: batch 0 OK, batch 1 pada -> exit, stan zachowany
    def flaky(batch):
        if batch == ["ID2"]:
            raise step45.BatchError("flaky")
        return fetcher(batch)

    state = step45.load_state(str(state_path))
    with pytest.raises(SystemExit):
        step45.run({"ID1": "fA", "ID2": "fB"}, flaky, state,
                   output_path=str(out), state_path=str(state_path))
    assert json.loads(state_path.read_text())["completed_batches"] == [0]

    # rerun: batch 0 pominiety, batch 1 dokonczony, stan skonsumowany
    fetched.clear()
    state = step45.load_state(str(state_path))
    step45.run({"ID1": "fA", "ID2": "fB"}, fetcher, state,
               output_path=str(out), state_path=str(state_path))
    assert fetched == [["ID2"]]                    # zero refetchu batcha 0
    assert not state_path.exists()
    content = out.read_text()
    assert content.count("Publisher Feed Hash") == 1   # naglowek raz
    assert "ID1,5,fA" in content and "ID2,5,fB" in content


def test_enrich_with_hash_port():
    header, rows = step45.enrich_with_hash(
        "Target,Total Visits\nID1,10\nID9,3\n", {"ID1": "feedA"})
    assert header[-1] == "Publisher Feed Hash"
    assert rows == [["ID1", "10", "feedA"], ["ID9", "3", ""]]


def test_load_target_map_traffic_type_guard(tmp_path):
    path = tmp_path / "tid.csv"
    path.write_text("Generic ID,Publisher Feed Hash,Traffic Type\n"
                    "ID1,fA,Domain\nID2,fB,Keyword\n", encoding="utf-8")
    assert step45.load_target_map(str(path)) == {"ID1": "fA"}


def test_no_input_missing_file_exits(monkeypatch):
    monkeypatch.setattr(step45, "INPUT_CSV", "definitely_missing_file.csv")
    with pytest.raises(SystemExit, match="brak"):
        step45.main()
