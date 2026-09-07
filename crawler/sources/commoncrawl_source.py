"""
Faza 3 — zrodlo `common_crawl` (Athena + WARC).

DECYZJA WYKONAWCZA: logika AWS portowana z v1 z chirurgicznymi fixami,
NIE przepisywana — dzialajacej infrastruktury Athena nie da sie testowac
poza srodowiskiem AWS, wiec rewrite bylby brawura. Fixy wzgledem v1:
  1. SQL: REPLACE(url_host_name,'www.','') kaleczyl domeny z 'www.'
     w srodku — teraz strip WYLACZNIE prefiksu (IF ... LIKE 'www.%').
  2. Normalizacja domen przez common.domains.normalize_domain.
  3. boto3 importowany leniwie (srodowiska bez AWS nie placa za import).
Parser i schemat: wspolne (schema.parse_html_to_schema).
"""

from __future__ import annotations

import gzip
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from common.domains import normalize_domain
from crawler.schema import empty_result, parse_html_to_schema

logger = logging.getLogger("crawler.common_crawl")

# Konfiguracja AWS czytana W CZASIE WYWOLANIA (fetch -> _reload_env), nie
# importu: entry laduje .env po imporcie pakietu, wiec odczyt modul-level
# zamrazalby puste wartosci (ta sama klasa bledu co late-binding sesji HTTP).
CC_DATABASE = "ccindex"
CC_TABLE = "ccindex"
CC_REGION = "us-east-1"
CC_S3_OUTPUT = ""


def _reload_env() -> None:
    """Odswieza konfiguracje z os.environ (po load_dotenv w entry)."""
    global CC_DATABASE, CC_TABLE, CC_REGION, CC_S3_OUTPUT
    CC_DATABASE = os.getenv("DATABASE", "ccindex")
    CC_TABLE = os.getenv("TABLE", "ccindex")
    CC_REGION = os.getenv("AWS_REGION", "us-east-1")
    CC_S3_OUTPUT = os.getenv("S3_OUTPUT", "")
    if CC_S3_OUTPUT and not CC_S3_OUTPUT.endswith("/"):
        CC_S3_OUTPUT += "/"

CC_CRAWLS = ["CC-MAIN-2026-04", "CC-MAIN-2025-49", "CC-MAIN-2025-44"]
CC_BATCH_SIZE = 200
CC_REQUEST_TIMEOUT = 60

ATHENA_COST_LOG = "athena_cost_log.csv"
COST_PER_TB = 5.0
BYTES_PER_TB = 1024 ** 4
MIN_BILLED_BYTES = 10 * (1024 ** 2)

# SQL: bezpieczny prefix-strip (fix bugu REPLACE z v1)
_HOST_NO_WWW = ("IF(url_host_name LIKE 'www.%', "
                "SUBSTR(url_host_name, 5), url_host_name)")


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=5,
                  status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; brand-crawler)"})
    return session


def _athena():
    import boto3
    return boto3.client("athena", region_name=CC_REGION)


def _s3():
    import boto3
    return boto3.client("s3", region_name=CC_REGION)


def _log_query_cost(query_name: str, execution_details) -> None:
    if not execution_details:
        return
    stats = execution_details.get("Statistics", {})
    scanned = stats.get("DataScannedInBytes", 0)
    billed = max(scanned, MIN_BILLED_BYTES) if scanned > 0 else 0
    cost = (billed / BYTES_PER_TB) * COST_PER_TB
    cumulative = cost
    if os.path.exists(ATHENA_COST_LOG):
        try:
            log_df = pd.read_csv(ATHENA_COST_LOG)
            if not log_df.empty and "Cumulative_Cost_USD" in log_df.columns:
                cumulative += log_df["Cumulative_Cost_USD"].iloc[-1]
        except Exception as exc:
            logger.error("Nie mozna odczytac cost logu: %s", exc)
    pd.DataFrame([{
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Query_Name": query_name,
        "Actual_Scanned_MB": round(scanned / (1024 ** 2), 4),
        "Billed_Cost_USD": round(cost, 6),
        "Cumulative_Cost_USD": round(cumulative, 6),
    }]).to_csv(ATHENA_COST_LOG, mode="a",
               header=not os.path.exists(ATHENA_COST_LOG), index=False)
    logger.info("Athena: $%.6f (lacznie $%.6f)", cost, cumulative)


ATHENA_MAX_WAIT_S = 600


def _wait_for_query(athena, execution_id: str, max_wait_s: int = ATHENA_MAX_WAIT_S):
    """Polling OGRANICZONY (fix nieskonczonej petli z v1): po max_wait_s
    query jest anulowane i zwracamy None — tier pada glosno, nie wisi."""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        response = athena.get_query_execution(QueryExecutionId=execution_id)
        state = response["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return response["QueryExecution"]
        if state in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get("StateChangeReason", "?")
            logger.error("Athena %s: %s", state, reason)
            return None
        time.sleep(2)
    logger.error("Athena: przekroczono %ss oczekiwania — anulujemy %s",
                 max_wait_s, execution_id)
    try:
        athena.stop_query_execution(QueryExecutionId=execution_id)
    except Exception:
        pass
    return None


def _setup_partitions(athena) -> None:
    partitions = " ".join(f"PARTITION (crawl='{c}', subset='warc')" for c in CC_CRAWLS)
    query = f"ALTER TABLE {CC_DATABASE}.{CC_TABLE} ADD IF NOT EXISTS {partitions}"
    response = athena.start_query_execution(
        QueryString=query, ResultConfiguration={"OutputLocation": CC_S3_OUTPUT})
    _log_query_cost("Setup Partitions", _wait_for_query(athena, response["QueryExecutionId"]))


def _batched_homepages(athena, s3, brands: list[str]) -> pd.DataFrame:
    brands_sql = ", ".join(f"'{b}'" for b in brands)
    crawls_sql = ", ".join(f"'{c}'" for c in CC_CRAWLS)
    case_sql = " ".join(f"WHEN '{c}' THEN {i + 1}" for i, c in enumerate(CC_CRAWLS))
    query = f"""
    WITH RankedHits AS (
        SELECT {_HOST_NO_WWW} AS brand, crawl, url, warc_filename,
               warc_record_offset AS offset, warc_record_length AS length,
               ROW_NUMBER() OVER (
                   PARTITION BY {_HOST_NO_WWW}
                   ORDER BY CASE crawl {case_sql} ELSE {len(CC_CRAWLS) + 1} END,
                            LENGTH(url_host_name) ASC
               ) AS rn
        FROM "{CC_DATABASE}"."{CC_TABLE}"
        WHERE subset = 'warc'
          AND crawl IN ({crawls_sql})
          AND {_HOST_NO_WWW} IN ({brands_sql})
          AND url_path IN ('/', '', '/index.html', '/index.htm', '/index.php')
          AND fetch_status = 200
    )
    SELECT brand, crawl, url, warc_filename, offset, length
    FROM RankedHits WHERE rn = 1;
    """
    response = athena.start_query_execution(
        QueryString=query, ResultConfiguration={"OutputLocation": CC_S3_OUTPUT})
    execution_id = response["QueryExecutionId"]
    details = _wait_for_query(athena, execution_id)
    if not details:
        return pd.DataFrame()
    _log_query_cost(f"Batch Homepages ({len(brands)})", details)

    path = CC_S3_OUTPUT.replace("s3://", "").split("/")
    bucket, prefix = path[0], "/".join(path[1:])
    local = Path(f"temp_{execution_id}.csv")
    s3.download_file(bucket, f"{prefix}{execution_id}.csv", str(local))
    frame = pd.read_csv(local)
    local.unlink(missing_ok=True)
    return frame


def _download_warc(row: dict, session: requests.Session) -> str | None:
    warc_url = f"https://data.commoncrawl.org/{row['warc_filename']}"
    offset, length = int(row["offset"]), int(row["length"])
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    try:
        response = session.get(warc_url, headers=headers, timeout=CC_REQUEST_TIMEOUT)
        if response.status_code in (200, 206):
            text = gzip.decompress(response.content).decode("utf-8", errors="ignore")
            parts = text.split("\r\n\r\n", 2)
            return parts[2] if len(parts) >= 3 else text
        logger.warning("WARC status %s: %s", response.status_code, warc_url)
    except Exception as exc:
        logger.error("WARC download failed: %s", exc)
    return None


async def fetch(domains: list[str]) -> list[dict]:
    _reload_env()
    if not CC_S3_OUTPUT:
        logger.warning("Brak S3_OUTPUT — Common Crawl pominiety.")
        return []
    athena, s3 = _athena(), _s3()
    _setup_partitions(athena)

    cleaned = sorted({normalize_domain(d) for d in domains if normalize_domain(d)})
    logger.info("Common Crawl: lookup %d domen (batch %d)", len(cleaned), CC_BATCH_SIZE)

    frames = []
    total_batches = (len(cleaned) + CC_BATCH_SIZE - 1) // CC_BATCH_SIZE
    for i in range(0, len(cleaned), CC_BATCH_SIZE):
        chunk = cleaned[i:i + CC_BATCH_SIZE]
        logger.info("Athena batch %d/%d (%d domen)...",
                    i // CC_BATCH_SIZE + 1, total_batches, len(chunk))
        frame = _batched_homepages(athena, s3, chunk)
        if not frame.empty:
            frames.append(frame)
        time.sleep(1)
    if not frames:
        return []

    coordinates = pd.concat(frames, ignore_index=True)
    session = _session()
    results: list[dict] = []
    records = coordinates.to_dict("records")
    for index, row in enumerate(records, 1):
        domain = row.get("brand", "unknown")
        logger.info("WARC %d/%d: %s", index, len(records), domain)
        url = row.get("url", f"https://{domain}")
        html = _download_warc(row, session)
        if not html:
            results.append(empty_result(domain, url, "WARC download failed"))
            continue
        parsed = parse_html_to_schema(html, domain, url)
        parsed["source"] = "common_crawl"
        results.append(parsed)
    return results
