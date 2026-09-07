"""
Faza 3 — zrodlo `wayback_machine` z oknem swiezosci i ograniczona
wspolbieznoscia.

Fixy wzgledem v1:
  1. Availability API pytany o snapshot najblizszy DZIS; snapshot starszy
     niz MAX_AGE_DAYS jest odrzucany (15-letnia strona = stare keywordy).
  2. ThreadPool zamiast sekwencyjnej petli (przy tysiacach brandow
     sekwencyjnie = godziny).
Parser i schemat: wspolne.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from common.domains import normalize_domain
from crawler.schema import empty_result, parse_html_to_schema

logger = logging.getLogger("crawler.wayback")

TIMEOUT = 60
MAX_AGE_DAYS = 730
CONCURRENCY = 8


def snapshot_is_fresh(timestamp: str, max_age_days: int = MAX_AGE_DAYS,
                      now: datetime | None = None) -> bool:
    """Czysta funkcja (testowana): czy snapshot YYYYMMDD... miesci sie w oknie."""
    if not timestamp or len(timestamp) < 8:
        return False
    try:
        taken = datetime.strptime(timestamp[:8], "%Y%m%d")
    except ValueError:
        return False
    reference = now or datetime.now()
    return (reference - taken) <= timedelta(days=max_age_days)


def _session() -> requests.Session:
    session = requests.Session()
    # Hotfix 3.3: v1-owe total=5/backoff=5 dawalo MINUTY na oporny request;
    # granica tieru wazniejsza niz heroiczne retry (kaskada i tak jest nizej).
    retry = Retry(total=3, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; brand-crawler)"})
    return session


def _fetch_one(domain: str, session: requests.Session,
               max_age_days: int) -> dict:
    clean = normalize_domain(domain)
    now_ts = datetime.now().strftime("%Y%m%d")
    api_url = f"https://archive.org/wayback/available?url={clean}&timestamp={now_ts}"
    try:
        response = session.get(api_url, timeout=TIMEOUT)
        response.raise_for_status()
        snapshot = response.json().get("archived_snapshots", {}).get("closest") or {}
    except Exception as exc:
        return empty_result(clean, f"https://{clean}", f"wayback_api: {str(exc)[:120]}")

    timestamp = snapshot.get("timestamp", "")
    if not snapshot.get("available") or not timestamp:
        return empty_result(clean, f"https://{clean}", "No snapshot in Wayback")
    if not snapshot_is_fresh(timestamp, max_age_days):
        return empty_result(clean, f"https://{clean}",
                            f"Snapshot too old ({timestamp[:8]}, okno {max_age_days}d)")

    raw_url = f"https://web.archive.org/web/{timestamp}id_/http://{clean}"
    try:
        page = session.get(raw_url, timeout=TIMEOUT)
        if page.status_code != 200:
            return empty_result(clean, raw_url, f"wayback_download: {page.status_code}")
        html = page.text
    except Exception as exc:
        return empty_result(clean, raw_url, f"wayback_download: {str(exc)[:120]}")

    result = parse_html_to_schema(html, clean, f"https://{clean}")
    result["source"] = "wayback_machine"
    return result


async def fetch(domains: list[str], max_age_days: int = MAX_AGE_DAYS,
                concurrency: int = CONCURRENCY) -> list[dict]:
    if not domains:
        return []
    session = _session()
    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_fetch_one, d, session, max_age_days): d
                   for d in domains}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            verdict = result.get("status") if result.get("status") == "success"                 else f"error: {str(result.get('error'))[:60]}"
            logger.info("wayback %d/%d: %s -> %s",
                        done, len(domains), result.get("domain"), verdict)
            results.append(result)
    return results
