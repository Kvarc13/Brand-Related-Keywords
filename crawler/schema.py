"""
Faza 3 — schemat v2 kontekstu brandu + JEDEN wspolny parser HTML.

Konsoliduje dwa rozbiezne parsery v1 (reczny Playwright vs
parse_html_to_schema) w jedna funkcje: kazde zrodlo dostarcza surowy HTML,
parser produkuje identyczny, wzbogacony rekord. Nowe pola v2 wzgledem v1:

  site_name       og:site_name -> JSON-LD Organization -> heurystyka z title;
                  kluczowe dla brandow, gdzie domena != marka (stanley1913,
                  flyfrontier, titan.fitness).
  json_ld_summary skrot danych application/ld+json (nazwy/opisy/marki) —
                  content kuratorowany przez sam brand, najwyzszy S/N.
  nav_links       anchor-teksty nawigacji zbierane PRZED decompose —
                  kategorie i linie produktowe zyja wlasnie tam.
  h2_headings     kolekcje/linie produktowe czesciej w H2 niz H1.
  meta_keywords   SEO-martwe, ale gdy jest — gotowa lista, koszt zero.
  lang            html[lang] / og:locale (thomann.de, refurbed.de).
  internal_links  linki wewnetrzne (ta sama domena rejestrowalna) — baza
                  dla kanalu podstron.
  main_content    ujednolicone ~3000 znakow NIEZALEZNIE od zrodla
                  (decyzje o cieciu podejmuje Step 2, nie crawler).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common.domains import registrable_domain

MAIN_CONTENT_CHARS = 3000
JSON_LD_CHARS = 800
NAV_LINKS_CAP = 30
H1_CAP = 5
H2_CAP = 10
INTERNAL_LINKS_CAP = 40

_INTERESTING_LD_TYPES = {
    "organization", "corporation", "website", "webSite", "brand", "product",
    "itemlist", "onlinestore", "store", "localbusiness",
}


def empty_result(domain: str, url: str, error: str | None) -> dict:
    """Rekord v2 w stanie bledu/pustym — jedyny ksztalt w calym pipeline."""
    return {
        "domain": domain,
        "url": url,
        "title": "",
        "meta_description": "",
        "meta_keywords": "",
        "site_name": "",
        "lang": "",
        "h1_headings": [],
        "h2_headings": [],
        "nav_links": [],
        "json_ld_summary": "",
        "internal_links": [],
        "main_content": "",
        "subpages": [],
        "wikidata": {},
        "search_snippet": "",
        "status": "error",
        "error": error,
        "source": "",
        "timestamp": datetime.now().isoformat(),
    }


def _clean(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] if limit else text


def _site_name_from_title(title: str) -> str:
    """Heurystyka ostatniej szansy: pierwszy segment tytulu przed separatorem."""
    for sep in (" | ", " – ", " — ", " - ", " · ", " :: "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if 1 < len(head) <= 40:
                return head
    return title.strip()[:60]


def _walk_json_ld(node, names: list, descs: list) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, names, descs)
        return
    if not isinstance(node, dict):
        return
    node_type = str(node.get("@type", "")).lower()
    if node_type in _INTERESTING_LD_TYPES or "name" in node:
        for key in ("name", "alternateName"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                names.append(_clean(value, 80))
        desc = node.get("description")
        if isinstance(desc, str) and desc.strip():
            descs.append(_clean(desc, 200))
        brand = node.get("brand")
        if isinstance(brand, dict) and isinstance(brand.get("name"), str):
            names.append(_clean(brand["name"], 80))
    for key in ("@graph", "itemListElement", "mainEntity"):
        if key in node:
            _walk_json_ld(node[key], names, descs)


def _extract_json_ld(soup: BeautifulSoup) -> tuple[str, str]:
    """Zwraca (summary, pierwsza_nazwa_organizacji) z blokow ld+json."""
    names: list[str] = []
    descs: list[str] = []
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            _walk_json_ld(json.loads(raw), names, descs)
        except (json.JSONDecodeError, TypeError):
            continue  # zepsuty blok nie moze polozyc parsowania strony
    unique_names = list(dict.fromkeys(n for n in names if n))
    unique_descs = list(dict.fromkeys(d for d in descs if d))
    parts = []
    if unique_names:
        parts.append("names: " + "; ".join(unique_names[:8]))
    if unique_descs:
        parts.append("desc: " + " | ".join(unique_descs[:3]))
    summary = _clean(" || ".join(parts), JSON_LD_CHARS)
    first_org = unique_names[0] if unique_names else ""
    return summary, first_org


def parse_html_to_schema(html: str, domain: str, original_url: str) -> dict:
    """Parsuje surowy HTML do rekordu v2. Nigdy nie rzuca — bledy w polu error."""
    result = empty_result(domain, original_url, None)
    if not html:
        result["error"] = "Empty HTML content"
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")

        # --- lang ---
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            result["lang"] = _clean(html_tag["lang"], 12)
        if not result["lang"]:
            og_locale = soup.find("meta", attrs={"property": re.compile(r"^og:locale$", re.I)})
            if og_locale and og_locale.get("content"):
                result["lang"] = _clean(og_locale["content"], 12)

        # --- title / meta ---
        if soup.title and soup.title.string:
            result["title"] = _clean(soup.title.string, 200)
        meta_desc = (
            soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            or soup.find("meta", attrs={"property": re.compile(r"^og:description$", re.I)})
        )
        if meta_desc and meta_desc.get("content"):
            result["meta_description"] = _clean(meta_desc["content"], 600)
        meta_kw = soup.find("meta", attrs={"name": re.compile(r"^keywords$", re.I)})
        if meta_kw and meta_kw.get("content"):
            result["meta_keywords"] = _clean(meta_kw["content"], 300)

        # --- site_name: og -> JSON-LD -> heurystyka ---
        og_site = soup.find("meta", attrs={"property": re.compile(r"^og:site_name$", re.I)})
        if og_site and og_site.get("content"):
            result["site_name"] = _clean(og_site["content"], 80)
        result["json_ld_summary"], ld_org_name = _extract_json_ld(soup)
        if not result["site_name"] and ld_org_name:
            result["site_name"] = ld_org_name
        if not result["site_name"] and result["title"]:
            result["site_name"] = _site_name_from_title(result["title"])

        # --- nav_links: PRZED decompose (v1 wycinal nav jako szum — dla tego
        #     zadania anchor-teksty nawigacji to kategorie/linie produktowe) ---
        nav_texts: list[str] = []
        nav_containers = soup.find_all(["nav", "header"]) + soup.find_all(
            attrs={"role": "navigation"})
        for container in nav_containers:
            for anchor in container.find_all("a"):
                text = _clean(anchor.get_text(), 40)
                if 2 < len(text) <= 40:
                    nav_texts.append(text)
        result["nav_links"] = list(dict.fromkeys(nav_texts))[:NAV_LINKS_CAP]

        # --- internal_links (baza kanalu podstron) ---
        own_domain = registrable_domain(domain)
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
                continue
            absolute = urljoin(original_url, href)
            if absolute.startswith(("http://", "https://")) and \
                    registrable_domain(absolute) == own_domain:
                links.append(absolute.split("#", 1)[0])
        result["internal_links"] = list(dict.fromkeys(links))[:INTERNAL_LINKS_CAP]

        # --- naglowki ---
        result["h1_headings"] = [
            _clean(h.get_text(), 120) for h in soup.find_all("h1")
            if _clean(h.get_text())
        ][:H1_CAP]
        result["h2_headings"] = [
            _clean(h.get_text(), 120) for h in soup.find_all("h2")
            if _clean(h.get_text())
        ][:H2_CAP]

        # --- main content (po zdjeciu szumu; nav juz zebrany) ---
        for tag in soup(["script", "style", "noscript", "nav", "footer",
                         "header", "aside", "form"]):
            tag.decompose()
        result["main_content"] = _clean(
            soup.get_text(separator=" ", strip=True), MAIN_CONTENT_CHARS)

        result["status"] = "success"
    except Exception as exc:  # parser nie moze polozyc pipeline'u
        result["error"] = f"Parsing error: {exc}"

    return result
