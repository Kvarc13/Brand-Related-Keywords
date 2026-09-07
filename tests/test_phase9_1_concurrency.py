"""
Faza 9.1 — testy wspolbieznosci per-brand (offline, workers > 1).

FakeTransport ma skrypty kolejkowe (kolejnosc globalna), wiec dla testow
rownoleglych uzywamy BrandKeyedTransport: odpowiedz per brand odczytana
z tresci wiadomosci — niezaleznie od tego, ktory watek pyta pierwszy.
"""

import json
import re
import threading
from pathlib import Path

from keywordgen import runner
from keywordgen.models import GenerationResult, KeywordItem


def kw(keyword, spec="branded", type_="product"):
    return KeywordItem(keyword=keyword, type=type_, specificity=spec,
                       rationale="")


class BrandKeyedTransport:
    """Generacje kluczowane brandem z promptu; sedzia zawsze pusty-OK."""

    def __init__(self, per_brand: dict[str, GenerationResult],
                 failing: set[str] = frozenset()):
        self._per_brand = per_brand
        self._failing = failing
        self._lock = threading.Lock()
        self.concurrent_now = 0
        self.max_concurrent = 0
        self.seen_brands: set[str] = set()

    def _brand_from(self, messages) -> str:
        match = re.search(r"Brand: (\S+)", messages[-1]["content"])
        return match.group(1) if match else "?"

    def generate(self, messages):
        brand = self._brand_from(messages)
        with self._lock:
            self.seen_brands.add(brand)
            self.concurrent_now += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent_now)
        try:
            import time
            time.sleep(0.02)                       # okno na realna rownoleglosc
            if brand in self._failing:
                raise RuntimeError(f"symulowany pad brandu {brand}")
            return self._per_brand[brand], {"prompt_tokens": 1,
                                            "completion_tokens": 1}
        finally:
            with self._lock:
                self.concurrent_now -= 1

    def judge(self, messages, grounded):
        from keywordgen.models import JudgeResult
        return JudgeResult(verdicts=[]), {"prompt_tokens": 0,
                                          "completion_tokens": 0}


def _config(tmp_path: Path, workers: int) -> dict:
    return {"self_consistency_n": 2, "max_keywords": 8,
            "category_cap_ratio": 0.5, "min_keywords": 1,
            "min_brand_direct": 0, "judge_grounding": False,
            "workers": workers, "model": "test", "resume": True,
            "blocklist_file": "keywordgen/blocklist_transactional.txt",
            "context_json": str(tmp_path / "ctx.json"),
            "checkpoint_file": str(tmp_path / "ck.jsonl"),
            "cost_log": str(tmp_path / "cost.csv"),
            "output_csv": str(tmp_path / "out.csv")}


def _transport(brands, failing=frozenset()):
    return BrandKeyedTransport(
        {b: GenerationResult(brand_name=b, vertical="v",
                             keywords=[kw(f"prod{b.split('.')[0]}"),
                                       kw(f"tech{b.split('.')[0]}")])
         for b in brands},
        failing=failing)


BRANDS = [f"brand{i}.com" for i in range(6)]


def test_concurrent_run_all_brands_deterministic_output(tmp_path):
    Path(_config(tmp_path, 4)["context_json"]).write_text("[]", encoding="utf-8")
    transport = _transport(BRANDS)
    stats = runner.run_sync(BRANDS, _config(tmp_path, 4), transport)
    assert stats["errors"] == 0
    assert transport.max_concurrent > 1            # pula NAPRAWDE rownolegla
    out1 = Path(_config(tmp_path, 4)["output_csv"]).read_bytes()

    # checkpoint: kazda linia = poprawny JSON (zapis w watku glownym, zero szatkowania)
    ck_lines = Path(_config(tmp_path, 4)["checkpoint_file"]).read_text(
        encoding="utf-8").strip().splitlines()
    assert len(ck_lines) == 6
    assert all(json.loads(line)["brand"] in BRANDS for line in ck_lines)

    # determinizm: swiezy katalog, inna liczba workerow -> identyczne bajty CSV
    tmp2 = tmp_path / "run2"; tmp2.mkdir()
    Path(_config(tmp2, 1)["context_json"]).write_text("[]", encoding="utf-8")
    runner.run_sync(BRANDS, _config(tmp2, 1), _transport(BRANDS))
    assert out1 == Path(_config(tmp2, 1)["output_csv"]).read_bytes()


def test_one_brand_failure_does_not_kill_pool(tmp_path):
    Path(_config(tmp_path, 4)["context_json"]).write_text("[]", encoding="utf-8")
    stats = runner.run_sync(BRANDS, _config(tmp_path, 4),
                            _transport(BRANDS, failing={"brand3.com"}))
    assert stats["errors"] == 1                    # tylko padniety brand
    import csv as csv_mod
    with open(_config(tmp_path, 4)["output_csv"], encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))
    brands_in_output = {r["Brand_Domain"] for r in rows}
    assert "brand3.com" not in brands_in_output
    assert len(brands_in_output) == 5


def test_resume_mid_pool_skips_done(tmp_path):
    config = _config(tmp_path, 4)
    Path(config["context_json"]).write_text("[]", encoding="utf-8")
    # pre-checkpoint dwoch brandow (symulacja przerwania w srodku puli)
    for brand in BRANDS[:2]:
        runner.append_checkpoint(Path(config["checkpoint_file"]),
                                 runner.BrandOutcome(
                                     brand=brand, rows=[{
                                         "Brand_Domain": brand, "Keyword": "old",
                                         "Context_Used": "Yes", "Specificity": "branded",
                                         "Vertical": "v", "In_Context": "No",
                                         "Confidence": "high", "Judge": "not_required"}],
                                     vertical="v", context_used="Yes",
                                     usage={"prompt_tokens": 0, "completion_tokens": 0},
                                     dropped=[]))
    transport = _transport(BRANDS)
    stats = runner.run_sync(BRANDS, config, transport)
    assert stats["resumed"] == 2
    assert transport.seen_brands == set(BRANDS[2:])   # zero refetchu ukonczonych
