"""
Faza 3 — testy pakietu crawler (w calosci OFFLINE).

Pokrycie:
  1. Parser v2 na syntetycznym, bogatym HTML: wszystkie nowe pola
     (site_name z og / JSON-LD / heurystyki title, nav PRZED decompose,
     h2, lang, meta_keywords, internal_links, zepsuty JSON-LD nie wywala).
  2. Bramka jakosci: klasy bot/js/geo/parked/thin + eskalacja HTTP.
  3. Checkpoint: roundtrip + semantyka resume (dobre pomijane, bledy nie).
  4. Wayback: czysta funkcja okna swiezosci.
  5. Enrich: wybor podstron tozsamosciowych z internal_links.
  6. crawl_report: coverage v2 vs v1 (brakujace pola -> 0%).
  7. E2E orkiestratora bez sieci: fake session (rich/bot/thin domeny),
     tier plain_http, bramka, checkpoint, resume nie refetchuje dobrych.
"""

import asyncio
import json
from pathlib import Path

from crawler import checkpoint as ckpt
from crawler import enrich, orchestrator, quality
from crawler.schema import empty_result, parse_html_to_schema
from crawler.sources import http_source
from crawler.sources.wayback_source import snapshot_is_fresh
from datetime import datetime

from tools import crawl_report

RICH_HTML = """
<html lang="en-US"><head>
<title>Stanley 1913 | Built For Life</title>
<meta name="description" content="Legendary Quencher tumblers and drinkware.">
<meta name="keywords" content="quencher, tumbler, drinkware">
<meta property="og:site_name" content="Stanley 1913">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization",
 "name":"Stanley Black Brands","alternateName":"Stanley1913",
 "description":"Drinkware company since 1913"}
</script>
<script type="application/ld+json">{broken json!!</script>
</head><body>
<nav><a href="/quencher">Quencher</a><a href="/iceflow">IceFlow</a>
<a href="/about-us">About Us</a><a href="x">x</a></nav>
<header><a href="/collections/tumblers">Tumblers</a></header>
<h1>Built For Life</h1><h1>Since 1913</h1>
<h2>The Quencher H2.0</h2><h2>IceFlow Series</h2>
<p>Stanley makes legendary vacuum-insulated drinkware loved worldwide.
The Quencher tumbler keeps drinks cold for hours and hours on end.</p>
<a href="https://stanley1913.com/products/quencher">buy</a>
<a href="https://facebook.com/stanley">fb</a>
<a href="mailto:x@y.z">mail</a>
</body></html>
"""


# ----------------------------------------------------------------------
# 1. Parser v2
# ----------------------------------------------------------------------

def test_parser_v2_extracts_all_new_fields():
    result = parse_html_to_schema(RICH_HTML, "stanley1913.com",
                                  "https://stanley1913.com")
    assert result["status"] == "success"
    assert result["site_name"] == "Stanley 1913"            # og:site_name wygrywa
    assert result["lang"] == "en-US"
    assert result["meta_keywords"].startswith("quencher")
    assert "Quencher" in result["nav_links"] and "IceFlow" in result["nav_links"]
    assert "Tumblers" in result["nav_links"]                 # header tez jest nawigacja
    assert "x" not in result["nav_links"]                    # za krotkie
    assert result["h1_headings"] == ["Built For Life", "Since 1913"]
    assert "The Quencher H2.0" in result["h2_headings"]
    assert "Stanley Black Brands" in result["json_ld_summary"]   # mimo zepsutego bloku
    assert "Drinkware company" in result["json_ld_summary"]
    assert any(link.endswith("/products/quencher") for link in result["internal_links"])
    assert not any("facebook" in link for link in result["internal_links"])
    assert "vacuum-insulated" in result["main_content"]
    assert len(result["main_content"]) <= 3000


def test_parser_site_name_fallbacks():
    no_og = "<html><head><title>Titan Fitness | Strength Equipment</title></head><body></body></html>"
    result = parse_html_to_schema(no_og, "titan.fitness", "https://titan.fitness")
    assert result["site_name"] == "Titan Fitness"            # heurystyka z title

    ld_only = ('<html><head><title>x</title><script type="application/ld+json">'
               '{"@type":"Organization","name":"Aroma360 Inc"}</script></head>'
               "<body></body></html>")
    result = parse_html_to_schema(ld_only, "aroma360.com", "https://aroma360.com")
    assert result["site_name"] == "Aroma360 Inc"             # JSON-LD przed heurystyka


def test_parser_empty_html_is_error_record():
    result = parse_html_to_schema("", "x.com", "https://x.com")
    assert result["status"] == "error" and result["error"]


# ----------------------------------------------------------------------
# 2. Bramka jakosci + eskalacja
# ----------------------------------------------------------------------

def test_quality_classify_all_classes():
    assert quality.classify("please enable javascript to continue " * 10) == "js_required"
    assert quality.classify("checking your browser cloudflare " * 10) == "bot_protection"
    assert quality.classify("not available in your country " * 10) == "geo_block"
    assert quality.classify("this domain is for sale " * 10) == "parked"
    assert quality.classify("tiny") == "thin"
    assert quality.classify("perfectly fine brand content " * 10) == "ok"


def test_quality_gate_uses_status_and_reason():
    good = empty_result("a.com", "https://a.com", None)
    good["status"] = "success"
    good["main_content"] = "real product content about tumblers " * 10
    assert quality.is_result_good(good)
    assert quality.gate_reason(good) == "ok"

    escalated = empty_result("b.com", "https://b.com", "escalate")
    escalated["status"] = "escalate"
    assert not quality.is_result_good(escalated)
    assert quality.gate_reason(escalated) == "status:escalate"


def test_should_escalate_codes_and_patterns():
    assert quality.should_escalate(403, "x" * 1000)
    assert quality.should_escalate(None, "whatever")
    assert quality.should_escalate(200, "short")                       # chudy HTML
    assert quality.should_escalate(200, "please enable javascript" + "x" * 400)
    assert not quality.should_escalate(200, "normal page content " * 50)


# ----------------------------------------------------------------------
# 3. Checkpoint / resume
# ----------------------------------------------------------------------

def _good(domain: str) -> dict:
    result = empty_result(domain, f"https://{domain}", None)
    result["status"] = "success"
    result["main_content"] = "long enough brand content for the gate " * 5
    return result


def test_checkpoint_roundtrip_and_resume_semantics(tmp_path):
    path = tmp_path / "ck.jsonl"
    ckpt.append_result(path, _good("a.com"))
    ckpt.append_result(path, empty_result("b.com", "https://b.com", "boom"))
    ckpt.append_result(path, _good("A.COM"))          # duplikat po normalizacji

    entries = ckpt.load_checkpoint(path)
    assert len(entries) == 3
    good = ckpt.good_results_from(entries)
    assert set(good) == {"a.com"}                     # blad b.com NIE jest resolved
    assert good["a.com"]["domain"] == "a.com"         # pierwszy dobry wygrywa


def test_checkpoint_skips_corrupt_lines(tmp_path):
    path = tmp_path / "ck.jsonl"
    path.write_text('{"domain": "a.com"}\nNOT JSON\n{"domain": "b.com"}\n',
                    encoding="utf-8")
    assert len(ckpt.load_checkpoint(path)) == 2


# ----------------------------------------------------------------------
# 4. Wayback freshness (czysta funkcja)
# ----------------------------------------------------------------------

def test_wayback_freshness_window():
    now = datetime(2026, 9, 4)
    assert snapshot_is_fresh("20260101000000", 730, now)
    assert snapshot_is_fresh("20241001", 730, now)
    assert not snapshot_is_fresh("20200101000000", 730, now)   # 15-letnie strony out
    assert not snapshot_is_fresh("", 730, now)
    assert not snapshot_is_fresh("garbage!", 730, now)


# ----------------------------------------------------------------------
# 5. Podstrony tozsamosciowe
# ----------------------------------------------------------------------

def test_pick_subpage_urls_identity_only():
    links = [
        "https://x.com/blog/post-1",
        "https://x.com/about-us",
        "https://x.com/products?ref=nav",
        "https://x.com/cart",
        "https://x.com/our-story/",
    ]
    picked = enrich.pick_subpage_urls(links, limit=2)
    assert picked == ["https://x.com/about-us", "https://x.com/products?ref=nav"]


# ----------------------------------------------------------------------
# 6. crawl_report (v2 vs v1)
# ----------------------------------------------------------------------

def test_crawl_report_coverage_v2_vs_v1():
    v2 = _good("a.com")
    v2["site_name"] = "A"
    v2["nav_links"] = ["X"]
    v1_style = {"domain": "b.com", "title": "B", "meta_description": "",
                "h1_headings": [], "main_content": "text", "status": "success",
                "source": "web_scraper"}
    new_report = crawl_report.analyze([v2])
    old_report = crawl_report.analyze([v1_style])
    assert new_report["coverage"]["site_name"] == 1.0
    assert old_report["coverage"]["site_name"] == 0.0       # v1 nie ma pola -> 0%
    assert old_report["coverage"]["title"] == 1.0
    assert new_report["domains"] == {"a.com"}


# ----------------------------------------------------------------------
# 7. E2E orkiestratora bez sieci (fake session na tierze plain_http)
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int, text: str, url: str):
        self.status_code, self.text, self.url = status_code, text, url


class FakeSession:
    PAGES = {
        "https://rich.com": FakeResponse(200, RICH_HTML, "https://rich.com"),
        "https://botwall.com": FakeResponse(
            200, "checking your browser cloudflare " * 40, "https://botwall.com"),
        "https://thin.com": FakeResponse(200, "<html>hi</html>", "https://thin.com"),
    }

    def get(self, url, **kwargs):
        if url in self.PAGES:
            return self.PAGES[url]
        raise ConnectionError(f"no route: {url}")

    @property
    def headers(self):
        return {}


def test_orchestrator_e2e_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(http_source, "build_session", lambda: FakeSession())
    brands = tmp_path / "Brands.csv"
    brands.write_text("Brand\nrich.com\nbotwall.com\nthin.com\n", encoding="utf-8")

    config = {
        "brands_file": str(brands),
        "url_column": "Brand",
        "output_json": str(tmp_path / "out.json"),
        "failed_csv": str(tmp_path / "failed.csv"),
        "checkpoint_file": str(tmp_path / "ck.jsonl"),
        "resume": True,
        "pipeline_order": ["plain_http"],          # tylko tier 1 (offline)
        "http_concurrency": 2,
        "enable_subpages": False,                  # enrich wylaczony w E2E
        "enable_wikidata": False,
        "enable_search_snippet": False,
    }

    stats = asyncio.run(orchestrator.run(config))
    assert stats["good"] == 1 and stats["failed"] == 2
    assert stats["per_source"] == {"plain_http": 1}
    # powody bramki: botwall eskalowany (bot-wall -> should_escalate), thin chudy
    reasons = stats["gate_reasons"]
    assert reasons.get("plain_http:status:escalate", 0) == 2

    output = json.loads(Path(config["output_json"]).read_text(encoding="utf-8"))
    assert len(output) == 1 and output[0]["domain"] == "rich.com"
    assert output[0]["site_name"] == "Stanley 1913"          # schema v2 w finalnym JSON
    failed = Path(config["failed_csv"]).read_text(encoding="utf-8")
    assert "botwall.com" in failed and "thin.com" in failed

    # RESUME: drugi run nie refetchuje rich.com (fake session moze byc pusty)
    monkeypatch.setattr(http_source, "build_session",
                        lambda: FakeSession() and type("S", (), {
                            "get": lambda self, url, **kw: (_ for _ in ()).throw(
                                ConnectionError("refetch!")),
                            "headers": {}})())
    stats2 = asyncio.run(orchestrator.run(config))
    assert stats2["resumed"] == 1
    assert stats2["good"] == 1                               # rich.com z checkpointu
    output2 = json.loads(Path(config["output_json"]).read_text(encoding="utf-8"))
    assert output2[0]["domain"] == "rich.com"


# ----------------------------------------------------------------------
# 8. Common Crawl: konfiguracja czytana w runtime (fix kolejnosci importow)
# ----------------------------------------------------------------------

def test_commoncrawl_env_resolved_at_call_time(monkeypatch):
    """Import pakietu PRZED load_dotenv nie moze zamrozic pustego S3_OUTPUT."""
    from crawler.sources import commoncrawl_source as cc
    monkeypatch.setenv("S3_OUTPUT", "s3://bucket/prefix")   # bez slasha
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    cc._reload_env()
    assert cc.CC_S3_OUTPUT == "s3://bucket/prefix/"          # slash dodany
    assert cc.CC_REGION == "eu-west-1"
    monkeypatch.delenv("S3_OUTPUT")
    cc._reload_env()
    assert cc.CC_S3_OUTPUT == ""                             # brak -> jawny skip
