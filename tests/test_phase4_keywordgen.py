"""
Faza 4 — testy keywordgen (100% offline; API za szwem FakeTransport).
"""

import json
from pathlib import Path

from keywordgen import batch as batch_mode
from keywordgen import runner
from keywordgen import validators as v
from keywordgen.llm import FakeTransport
from keywordgen.models import (GenerationResult, JudgeResult, JudgeVerdict,
                               KeywordItem, to_strict_schema)
from keywordgen.prompts import build_context_block, generation_messages

BLOCKLIST = {"shipping", "checkout", "online"}


def kw(keyword, spec="branded", type_="product", rationale=""):
    return KeywordItem(keyword=keyword, type=type_, specificity=spec,
                       rationale=rationale)


# ----------------------------------------------------------------------
# 1. Walidatory: kazda regula
# ----------------------------------------------------------------------

def test_normalize_token_joins_and_lowers():
    assert v.normalize_token("Fire TV") == "firetv"
    assert v.normalize_token("American Express") == "americanexpress"
    assert v.normalize_token("e-commerce_x ") == "ecommercex"


def test_validate_each_rule():
    candidates = [
        v.Candidate("nikee", "branded", "third_party_brand"),      # cudza marka
        v.Candidate("premiumfeel", "weak", "other"),               # weak
        v.Candidate("café", "branded", "product"),                 # non-ascii
        v.Candidate("abc", "branded", "product"),                  # za krotki (3)
        v.Candidate("a" * 21, "branded", "product"),               # za dlugi
        v.Candidate("shipping", "category_strong", "category"),    # blocklista
        v.Candidate("kindle", "branded", "product"),
        v.Candidate("kindle", "branded", "product"),               # duplikat
        v.Candidate("weird", "mystery_tier", "product"),           # nieznany tier
    ]
    report = v.validate_candidates(candidates, BLOCKLIST)
    assert [c.keyword for c in report.kept] == ["kindle"]
    reasons = {c.keyword: c.rejected_reason for c in report.dropped}
    assert reasons["nikee"] == "trap:third_party_brand"
    assert reasons["premiumfeel"] == "trap:weak"
    assert reasons["café"] == "non_ascii_or_symbols"
    assert reasons["abc"] == "length:3"
    assert reasons["shipping"] == "blocklist"
    assert "duplicate" in reasons.values()
    assert reasons["weird"].startswith("unknown_specificity")


def test_inject_sld_always_present_even_short():
    # hims.com: SLD 'hims' (4) — fix straty brandu z v1; gilt analogicznie
    injected = v.inject_sld([], "hims.com")
    assert injected[0].keyword == "hims"
    assert injected[0].specificity == "brand_core"
    assert injected[0].rationale.startswith("injected SLD")
    # gdy model sam dal SLD — podbicie do brand_core, bez duplikatu
    existing = [v.Candidate("gilt", "branded", "brand", confidence="low")]
    result = v.inject_sld(existing, "gilt.com")
    assert len(result) == 1
    assert result[0].specificity == "brand_core" and result[0].confidence == "high"


def test_merge_generations_core_vs_lowconf():
    gen_a = [kw("kindle").model_dump(), kw("alexa").model_dump(),
             kw("Fire TV").model_dump()]
    gen_b = [kw("kindle").model_dump(), kw("firetv").model_dump(),
             kw("prime").model_dump()]
    merged = v.merge_generations([gen_a, gen_b])
    by_token = {c.keyword: c for c in merged}
    assert by_token["kindle"].confidence == "high"
    assert by_token["firetv"].confidence == "high"    # 'Fire TV' sklejone = przeciecie
    assert by_token["alexa"].confidence == "low"
    assert by_token["prime"].confidence == "low"


def test_priority_fill_tiers_and_category_cap():
    candidates = [
        v.Candidate("cat1", "category_strong", "category"),
        v.Candidate("cat2", "category_strong", "category"),
        v.Candidate("cat3", "category_strong", "category"),
        v.Candidate("core1", "brand_core", "brand"),
        v.Candidate("brand1", "branded", "product"),
        v.Candidate("brand2", "branded", "product", confidence="low"),
    ]
    selected = v.priority_fill(candidates, max_keywords=4, category_cap_ratio=0.5)
    tokens = [c.keyword for c in selected]
    assert tokens[0] == "core1"                        # Tier1 przed Tier2
    assert tokens[1] == "brand1" and tokens[2] == "brand2"  # high przed low w tierze
    assert sum(1 for c in selected if c.specificity == "category_strong") <= 2
    assert len(selected) == 4
    # cap nigdy nieprzekroczony nawet przy niedoborze Tier1: sloty zostaja puste
    only_categories = [v.Candidate(f"cat{i}", "category_strong", "category")
                       for i in range(6)]
    selected = v.priority_fill(only_categories, max_keywords=8, category_cap_ratio=0.5)
    assert len(selected) == 4


def test_judge_gating_and_verdicts():
    candidates = [
        v.Candidate("kindle", "brand_core", "brand", confidence="high"),
        v.Candidate("prime", "branded", "product", confidence="low"),
        v.Candidate("ecommerce", "category_strong", "category", confidence="high"),
        v.Candidate("hims", "brand_core", "brand",
                     rationale="injected SLD (gwarancja tokenu brandu)"),
    ]
    items = v.keywords_needing_judge(candidates, brand_is_fallback=False)
    questions = {i["keyword"]: i["question"] for i in items}
    assert questions == {"prime": "NAV", "ecommerce": "CAT"}   # SLD i pewny core poza sedzia

    # fail-closed dla ocenianych
    kept = v.apply_judge_verdicts(candidates, {"prime": True, "ecommerce": False},
                                  judged_tokens={"prime", "ecommerce"})
    tokens = [c.keyword for c in kept]
    assert "prime" in tokens and "ecommerce" not in tokens
    assert next(c for c in kept if c.keyword == "prime").judge == "accepted"

    # blad sedziego -> nie-destrukcyjnie, judge=error
    kept = v.apply_judge_verdicts(candidates, None, judged_tokens={"ecommerce"})
    marked = next(c for c in kept if c.keyword == "ecommerce")
    assert marked.judge == "error"


def test_mark_in_context():
    candidates = [v.Candidate("kindle", "branded", "product"),
                  v.Candidate("quencher", "branded", "product")]
    v.mark_in_context(candidates, "Buy the new Kindle Paperwhite today")
    assert candidates[0].in_context and not candidates[1].in_context


def test_blocklist_file_loads():
    words = v.load_blocklist(Path("keywordgen/blocklist_transactional.txt"))
    assert "shipping" in words and "checkout" in words
    assert "rewards" not in words          # strefa kalibracji poza lista
    assert "marketplace" not in words
    assert all(not w.startswith("#") for w in words)


# ----------------------------------------------------------------------
# 2. Kontekst v2 i strict schema
# ----------------------------------------------------------------------

def test_context_block_uses_v2_fields():
    record = {"site_name": "Stanley 1913", "title": "T", "nav_links": ["Quencher"],
              "json_ld_summary": "names: Stanley", "h2_headings": ["IceFlow"],
              "wikidata": {"name": "Stanley", "description": "drinkware",
                           "aliases": ["Stanley PMI"]},
              "subpages": [{"title": "About", "text": "Founded 1913" * 3}],
              "main_content": "X" * 5000}
    block = build_context_block(record)
    for fragment in ("site_name: Stanley 1913", "nav: Quencher", "h2: IceFlow",
                     "json_ld", "wikidata", "subpage[About]"):
        assert fragment in block
    assert len([l for l in block.splitlines() if l.startswith("content:")]) == 1
    assert len(block) < 6000               # cap contentu dziala


def test_strict_schema_shape():
    schema = to_strict_schema(GenerationResult)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    item_schema = schema["$defs"]["KeywordItem"]
    assert item_schema["additionalProperties"] is False
    assert "keyword" in item_schema["required"]


# ----------------------------------------------------------------------
# 3. E2E process_brand na FakeTransport (w tym retry)
# ----------------------------------------------------------------------

CONFIG = {"self_consistency_n": 2, "max_keywords": 8, "category_cap_ratio": 0.5,
          "min_keywords": 4, "min_brand_direct": 2, "judge_grounding": True}

CONTEXT_RECORD = {"domain": "amazon.com", "status": "success",
                  "title": "Amazon", "meta_description": "shop",
                  "main_content": "Kindle Alexa FireTV Prime shopping",
                  "source": "web_scraper"}


def _generation(*items):
    return GenerationResult(brand_name="Amazon", vertical="e-commerce",
                            keywords=list(items))


def test_process_brand_happy_path():
    transport = FakeTransport(
        generations=[
            _generation(kw("Kindle"), kw("Alexa"), kw("ecommerce", "category_strong", "category"),
                        kw("nikee", type_="third_party_brand")),
            _generation(kw("Kindle"), kw("Alexa"), kw("ecommerce", "category_strong", "category")),
        ],
        judge_results=[JudgeResult(verdicts=[JudgeVerdict(keyword="ecommerce", accept=True)])],
    )
    outcome = runner.process_brand("amazon.com", CONTEXT_RECORD, transport,
                                   BLOCKLIST, CONFIG)
    assert outcome.error == "" and not outcome.retried
    tokens = [r["Keyword"] for r in outcome.rows]
    assert "amazon" in tokens              # wstrzykniety SLD
    assert "kindle" in tokens and "alexa" in tokens and "ecommerce" in tokens
    assert "nikee" not in tokens           # pulapka odcieta
    assert any(d.startswith("nikee(trap:third_party_brand") for d in outcome.dropped)
    row = next(r for r in outcome.rows if r["Keyword"] == "kindle")
    assert row["Context_Used"] == "Yes" and row["In_Context"] == "Yes"
    assert row["Vertical"] == "e-commerce" and row["Confidence"] == "high"
    # sedzia: brand z kontekstem -> bez groundingu
    assert transport.judge_calls[0][1] is False


def test_process_brand_retry_on_thin_result():
    thin = _generation(kw("premiumfeel", "weak", "other"))       # wszystko wypadnie
    retry_generation = _generation(kw("Kindle"), kw("Alexa"), kw("FireTV"),
                                   kw("Prime"))
    transport = FakeTransport(generations=[thin, thin, retry_generation],
                              judge_results=[])
    outcome = runner.process_brand("amazon.com", CONTEXT_RECORD, transport,
                                   BLOCKLIST, CONFIG)
    assert outcome.retried
    tokens = [r["Keyword"] for r in outcome.rows]
    assert {"kindle", "alexa", "firetv", "prime", "amazon"} <= set(tokens)
    assert len(transport.generate_calls) == 3
    # feedback retry poszedl do modelu
    assert "Previous attempt" in transport.generate_calls[-1][-1]["content"]


def test_process_brand_fallback_uses_grounded_judge():
    transport = FakeTransport(
        generations=[
            _generation(kw("sneakers", "category_strong", "category")),
            _generation(kw("sneakers", "category_strong", "category")),
            _generation(kw("sneakers", "category_strong", "category"),
                        kw("footwear", "category_strong", "category")),
        ],
        judge_results=[
            JudgeResult(verdicts=[JudgeVerdict(keyword="sneakers", accept=True)]),
            JudgeResult(verdicts=[JudgeVerdict(keyword="sneakers", accept=True),
                                  JudgeVerdict(keyword="footwear", accept=True)]),
        ],
    )
    outcome = runner.process_brand("unknownshoes.com", None, transport,
                                   BLOCKLIST, CONFIG)
    assert outcome.context_used == "No (Fallback)"
    assert transport.judge_calls[0][1] is True         # grounding dla fallbacku
    assert any(r["Keyword"] == "unknownshoes" for r in outcome.rows)


def test_process_brand_judge_error_keeps_with_flag():
    generation = _generation(kw("Kindle"), kw("Alexa"),
                             kw("ecommerce", "category_strong", "category"))
    transport = FakeTransport(
        generations=[generation, generation],
        judge_results=[None],                          # symulowany blad sedziego
    )
    outcome = runner.process_brand("amazon.com", CONTEXT_RECORD, transport,
                                   BLOCKLIST, CONFIG)
    assert outcome.error == "" and not outcome.retried
    row = next(r for r in outcome.rows if r["Keyword"] == "ecommerce")
    assert row["Judge"] == "error"                     # nie-destrukcyjnie


def test_process_brand_transport_crash_is_brand_error_not_run_crash():
    outcome = runner.process_brand("amazon.com", CONTEXT_RECORD,
                                   FakeTransport(), BLOCKLIST, CONFIG)
    assert outcome.error and outcome.rows == []


# ----------------------------------------------------------------------
# 4. Checkpoint / resume / kontrakt CSV
# ----------------------------------------------------------------------

def test_run_sync_checkpoint_resume_and_contract(tmp_path):
    config = dict(CONFIG)
    config.update({
        "blocklist_file": "keywordgen/blocklist_transactional.txt",
        "context_json": str(tmp_path / "ctx.json"),
        "checkpoint_file": str(tmp_path / "ck.jsonl"),
        "cost_log": str(tmp_path / "cost.csv"),
        "output_csv": str(tmp_path / "out.csv"),
        "resume": True, "model": "test",
    })
    Path(config["context_json"]).write_text(json.dumps([CONTEXT_RECORD]),
                                            encoding="utf-8")
    transport = FakeTransport(
        generations=[_generation(kw("Kindle"), kw("Alexa"), kw("FireTV"),
                                 kw("Prime"))] * 2)
    stats = runner.run_sync(["amazon.com"], config, transport)
    assert stats["rows"] >= 4 and stats["errors"] == 0

    # kontrakt: kolumny legacy pierwsze — Step3 przenosi, Step5/eval czytaja po nazwach
    header = Path(config["output_csv"]).read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("Brand_Domain,Keyword,Context_Used")
    for column in ("Specificity", "Vertical", "In_Context", "Confidence", "Judge"):
        assert column in header

    # resume: pusty transport => run przechodzi w calosci z checkpointu
    stats2 = runner.run_sync(["amazon.com"], config, FakeTransport())
    assert stats2["resumed"] == 1 and stats2["rows"] == stats["rows"]


# ----------------------------------------------------------------------
# 5. Batch: builder i parser (roundtrip offline)
# ----------------------------------------------------------------------

def test_batch_build_and_parse_roundtrip(tmp_path):
    context_lookup = {"amazon.com": CONTEXT_RECORD}
    config = dict(CONFIG); config["model"] = "gpt-5-mini"
    requests_out = batch_mode.build_requests(["amazon.com", "hims.com"],
                                             context_lookup, config)
    assert len(requests_out) == 4                       # 2 brandy x n=2
    assert requests_out[0]["custom_id"] == "amazon.com||0"
    body = requests_out[0]["body"]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["url" ] if False else requests_out[0]["url"] == "/v1/chat/completions"

    fake_line = json.dumps({
        "custom_id": "amazon.com||1",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content":
                _generation(kw("Kindle")).model_dump_json()}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}},
    })
    brand, index, result, usage = batch_mode.parse_output_line(fake_line)
    assert brand == "amazon.com" and index == 1
    assert result.keywords[0].keyword == "Kindle"
    assert usage == {"prompt_tokens": 7, "completion_tokens": 3}

    bad_line = json.dumps({"custom_id": "x||0",
                           "response": {"status_code": 500, "body": {}}})
    _, _, result, _ = batch_mode.parse_output_line(bad_line)
    assert result is None


def test_prefetched_transport_delegates_retry_and_judge():
    real = FakeTransport(generations=[_generation(kw("Prime"))],
                         judge_results=[JudgeResult(verdicts=[])])
    prefetched = batch_mode.PrefetchedTransport([_generation(kw("Kindle"))], real)
    first, usage = prefetched.generate([])
    assert first.keywords[0].keyword == "Kindle" and usage["prompt_tokens"] == 0
    second, _ = prefetched.generate([])                # wyczerpane -> sync
    assert second.keywords[0].keyword == "Prime"
    prefetched.judge([], grounded=True)
    assert real.judge_calls
