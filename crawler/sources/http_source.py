"""
Faza 3 — zrodlo `plain_http`: tani HTTP GET przed przegladarka.

Wzorzec eskalacyjny z projektu hackathonowego: zwykly requests zalatwia
duza czesc domen za grosze; Playwright startuje tylko dla eskalowanych
(statusy z ESCALATE_STATUS_CODES, wykryty js-required/bot-wall, chudy HTML).
Eskalacja dzieje sie NATURALNIE przez bramke jakosci orkiestratora —
wynik z powodem != 'ok' nie jest dobry, wiec domena spada do nastepnego
zrodla. SSL fallback jak w zipie: pierwszy SSLError -> retry verify=False.

`session_factory` jest wstrzykiwalne — testy podmieniaja na fake bez sieci.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from crawler.quality import should_escalate
from crawler.schema import empty_result, parse_html_to_schema

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
TIMEOUT = 20
RETRIES = 2


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=RETRIES, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods={"GET"})
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_one(domain: str, session_factory=None) -> dict:
    """Pobiera jedna domene; wynik ZAWSZE w schemacie v2 (nigdy nie rzuca).

    session_factory wiazany W CZASIE WYWOLANIA (late binding) — default
    zwiazany przy definicji uniemozliwialby wstrzykniecie fake'a w testach.
    """
    if session_factory is None:
        session_factory = build_session
    url = str(domain).strip().lower()
    if not url.startswith("http"):
        url = "https://" + url

    session = session_factory()
    try:
        try:
            response = session.get(url, timeout=TIMEOUT)
        except requests.exceptions.SSLError:
            response = session.get(url, timeout=TIMEOUT, verify=False)
        status_code, html, final_url = response.status_code, response.text, str(response.url)
    except Exception as exc:
        result = empty_result(domain, url, f"http_error: {str(exc)[:150]}")
        result["source"] = "plain_http"
        return result

    if should_escalate(status_code, html):
        result = empty_result(domain, final_url, f"escalate: status={status_code}")
        result["status"] = "escalate"
        result["source"] = "plain_http"
        return result

    result = parse_html_to_schema(html, domain, final_url)
    result["source"] = "plain_http"
    return result


def fetch(domains: list[str], concurrency: int = 16,
          session_factory=None) -> list[dict]:
    results: list[dict] = []
    if not domains:
        return results
    if session_factory is None:
        session_factory = build_session
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(fetch_one, d, session_factory): d for d in domains}
        for future in as_completed(futures):
            results.append(future.result())
    return results
