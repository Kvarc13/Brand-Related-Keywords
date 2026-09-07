"""
Faza 3 — kanaly opcjonalne wzbogacania kontekstu (Q8: wszystkie za flagami,
domyslnie wlaczone; kazdy degraduje sie GRACEFUL — brak sieci/pakietu/
danych nigdy nie psuje glownego wyniku).

  subpages       1-2 podstrony tozsamosciowe (about/products) — TYLKO tanim
                 tierem HTTP, nigdy przegladarka; kandydaci z internal_links.
  wikidata       reverse-lookup po P856 (official website): nazwa, aliasy,
                 opis — ustrukturyzowane, odporne na scraping; brak wpisu
                 dla malych brandow = pusty dict.
  search_snippet srodkowy tier fallbacku dla brandow BEZ kontekstu — snippet
                 z wyszukiwarki redukuje halucynacje trybu czystej wiedzy.
                 Wymaga pakietu `ddgs` (import-guard: brak = skip z logiem).
"""

from __future__ import annotations

import logging
import re

import requests

from crawler.patterns import SUBPAGE_PATTERNS
from crawler.schema import _clean  # wspolna normalizacja bialych znakow

logger = logging.getLogger("crawler.enrich")

SUBPAGE_TEXT_CHARS = 1500
SUBPAGE_MAX = 2
SUBPAGE_TIMEOUT = 10
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_TIMEOUT = 10


def build_fast_session() -> requests.Session:
    """Sesja do wzbogacania: BEZ retry (kanaly opcjonalne nie moga
    multiplikowac czasu runu; brak podstrony to strata 10 s, nie 60 s)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "brand-context-crawler/1.0"})
    return session

_SUBPAGE_RE = re.compile("|".join(SUBPAGE_PATTERNS), re.IGNORECASE)


def pick_subpage_urls(internal_links: list[str], limit: int = SUBPAGE_MAX) -> list[str]:
    """Czysta funkcja (testowana): kandydaci na podstrony tozsamosciowe."""
    picked: list[str] = []
    for link in internal_links or []:
        path = link.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        path = path.split("?", 1)[0]
        if _SUBPAGE_RE.search(path):
            picked.append(link)
        if len(picked) >= limit:
            break
    return picked


def fetch_subpages(result: dict, session: requests.Session,
                   limit: int = SUBPAGE_MAX) -> None:
    """Dopina result['subpages']; bledy per-URL logowane, nie propagowane."""
    from bs4 import BeautifulSoup
    for url in pick_subpage_urls(result.get("internal_links", []), limit):
        try:
            response = session.get(url, timeout=SUBPAGE_TIMEOUT)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            title = _clean(soup.title.string if soup.title and soup.title.string else "", 150)
            text = _clean(soup.get_text(separator=" ", strip=True), SUBPAGE_TEXT_CHARS)
            if text:
                result["subpages"].append({"url": url, "title": title, "text": text})
        except Exception as exc:
            logger.info("subpage skip %s: %s", url, str(exc)[:80])


def _wikidata_query(domain: str) -> str:
    variants = " ".join(
        f"<{scheme}://{prefix}{domain}{suffix}>"
        for scheme in ("http", "https")
        for prefix in ("", "www.")
        for suffix in ("", "/")
    )
    return f"""
    SELECT ?itemLabel ?itemDescription
           (GROUP_CONCAT(DISTINCT ?alias; separator="; ") AS ?aliases) WHERE {{
      VALUES ?site {{ {variants} }}
      ?item wdt:P856 ?site.
      OPTIONAL {{ ?item skos:altLabel ?alias. FILTER(LANG(?alias) = "en") }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} GROUP BY ?itemLabel ?itemDescription LIMIT 1
    """


def fetch_wikidata(domain: str, session: requests.Session) -> dict:
    try:
        response = session.get(
            WIKIDATA_ENDPOINT,
            params={"query": _wikidata_query(domain), "format": "json"},
            timeout=WIKIDATA_TIMEOUT,
            headers={"User-Agent": "brand-context-crawler/1.0"},
        )
        response.raise_for_status()
        bindings = response.json().get("results", {}).get("bindings", [])
        if not bindings:
            return {}
        row = bindings[0]
        aliases = (row.get("aliases", {}).get("value") or "").split("; ")
        return {
            "name": row.get("itemLabel", {}).get("value", ""),
            "description": row.get("itemDescription", {}).get("value", ""),
            "aliases": [a for a in aliases if a][:5],
        }
    except Exception as exc:
        logger.info("wikidata skip %s: %s", domain, str(exc)[:80])
        return {}


def fetch_search_snippet(brand: str) -> str:
    """Snippet z DDG dla brandow bez kontekstu; wymaga pakietu ddgs."""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.info("search_snippet: brak pakietu ddgs (pip install ddgs) — skip")
        return ""
    try:
        results = DDGS().text(f"{brand} official site", max_results=1)
        if results:
            return _clean(results[0].get("body") or results[0].get("title") or "", 400)
    except Exception as exc:
        logger.info("search_snippet skip %s: %s", brand, str(exc)[:80])
    return ""
