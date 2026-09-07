"""
Keyword_Step4_5_By_Target_Consecutive.py  (Faza 7 — v2)
=======================================================
Pobiera 30-dniowe statystyki targetow z Zeropark Reports API.

RUNBOOK: uruchamiac z wnetrza VPN (auth = VPN+SSO, decyzja Q2 — zadnych
kluczy w kodzie). Zmiany vs v1 (legacy/...v1.py) — sama ODPORNOSC:
  1. timeout= na KAZDYM reqeuscie (v1: brak -> ryzyko wiszenia).
  2. Polling statusu OGRANICZONY (max prob + interwal) — v1: while True
     na wiecznym 404 wisial na zawsze.
  3. Nieudany batch: 1 retry, potem GLOSNY fail z exit 1 — v1 robil
     `continue`, zostawiajac ciche dziury w TargetData i zanizony raport.
  4. Resume per batch: stan w step45_state.json; restart pomija ukonczone
     batche i dopisuje do istniejacego outputu.
  5. Bez input() (v1 lamal runy nienadzorowane); brak pliku = sys.exit.
Filtr Traffic Type == 'Domain' zostaje jako tani guard (Step 4 dzis
hardcoduje te wartosc, ale zrodla TargetID moga sie zmienic).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

API_BASE_URL = "https://reports-api.zeropark.codewise.com"
REPORT_NAME = "TimeTargetRow"
INPUT_CSV = "TargetID.csv"
OUTPUT_CSV = "TargetData.csv"
STATE_FILE = "step45_state.json"

DATE_RANGE = "LAST_30_DAYS"
TIME_AGGREGATION = "none"
TIME_OFFSET = "Z"
COLUMNS_TO_INCLUDE = ["TARGET", "TARGET_HASH", "TARGET_ADDRESS",
                      "TOTAL_VISITS", "AVERAGE_COST", "SOLD_VISITS"]

CHUNK_SIZE = 10_000
REQUEST_TIMEOUT = 60          # sekundy, kazdy request
POLL_INTERVAL_S = 10
POLL_MAX_ATTEMPTS = 90        # 90 x 10 s = 15 min na batch
BATCH_RETRIES = 1             # ponowien nieudanego batcha przed failem


class BatchError(Exception):
    pass


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


# ==========================================
# WEJSCIE
# ==========================================

def load_target_map(file_path: str) -> dict[str, str]:
    """{Generic ID: Publisher Feed Hash} dla wierszy Traffic Type == Domain."""
    target_map: dict[str, str] = {}
    with open(file_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Generic ID", "Publisher Feed Hash", "Traffic Type"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"ERROR: {file_path} bez kolumn: {missing}")
        total = 0
        for row in reader:
            total += 1
            if row.get("Traffic Type", "").strip() != "Domain":
                continue
            gid = (row.get("Generic ID") or "").strip()
            pfh = (row.get("Publisher Feed Hash") or "").strip()
            if gid:
                target_map[gid] = pfh
    print(f"Wczytano {total} wierszy; {len(target_map)} targetow Domain.")
    return target_map


# ==========================================
# API (odpornosc: timeouty + bounded polling)
# ==========================================

def schedule_report(session: requests.Session, target_batch: list[str]) -> str:
    payload = {"reportName": REPORT_NAME, "timeAggregation": TIME_AGGREGATION,
               "timeOffset": TIME_OFFSET,
               "filters": [{"name": "TARGET", "values": target_batch}],
               "dateRange": DATE_RANGE, "columns": COLUMNS_TO_INCLUDE}
    response = session.post(f"{API_BASE_URL}/report", json=payload,
                            timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise BatchError(f"schedule: {response.status_code} {response.text[:200]}")
    status_url = response.json().get("message")
    if not status_url:
        raise BatchError("schedule: brak status URL w odpowiedzi")
    return status_url


def wait_for_download_url(session: requests.Session, status_url: str,
                          max_attempts: int = POLL_MAX_ATTEMPTS,
                          sleep=time.sleep) -> str:
    """Bounded polling (fix wiecznej petli v1): po max_attempts -> BatchError."""
    for attempt in range(1, max_attempts + 1):
        response = session.get(status_url, allow_redirects=False,
                               timeout=REQUEST_TIMEOUT)
        if response.status_code == 302:
            return response.headers.get("Location") or ""
        if response.status_code != 404:
            raise BatchError(f"status: nieoczekiwany {response.status_code}")
        sleep(POLL_INTERVAL_S)
    raise BatchError(f"status: raport niegotowy po {max_attempts} probach "
                     f"({max_attempts * POLL_INTERVAL_S}s)")


def fetch_report_csv(session: requests.Session, target_batch: list[str]) -> str:
    status_url = schedule_report(session, target_batch)
    download_url = wait_for_download_url(session, status_url)
    if not download_url:
        raise BatchError("download: pusty Location")
    response = session.get(download_url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise BatchError(f"download: {response.status_code}")
    return response.text


# ==========================================
# WYJSCIE (port z v1) + STAN RESUME
# ==========================================

def find_column_index(header: list[str], name: str) -> int | None:
    target = name.strip().lower()
    for i, col in enumerate(header):
        if col.strip().lower() == target:
            return i
    return None


def enrich_with_hash(csv_text: str, target_map: dict[str, str]):
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return None, []
    original_header = rows[0]
    header = original_header + ["Publisher Feed Hash"]
    target_idx = find_column_index(original_header, "Target")
    if target_idx is None:
        print(f"UWAGA: brak kolumny Target w odpowiedzi API: {original_header}")
    data_rows = []
    for row in rows[1:]:
        if target_idx is not None and target_idx < len(row):
            pub_hash = target_map.get(row[target_idx].strip(), "")
        else:
            pub_hash = ""
        data_rows.append(row + [pub_hash])
    return header, data_rows


def load_state(path: str = STATE_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"completed_batches": [], "header_written": False}


def save_state(state: dict, path: str = STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def write_batch(output_path: str, header, data_rows, write_header: bool) -> None:
    with open(output_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header and header:
            writer.writerow(header)
        writer.writerows(data_rows)


# ==========================================
# ORKIESTRACJA (resume + fail-loud)
# ==========================================

def run(target_map: dict[str, str], fetcher, state: dict,
        output_path: str = OUTPUT_CSV, state_path: str = STATE_FILE) -> int:
    """fetcher(batch)->csv_text wstrzykiwalny (testy). Zwraca liczbe batchy."""
    all_ids = list(target_map.keys())
    batches = [all_ids[i:i + CHUNK_SIZE] for i in range(0, len(all_ids), CHUNK_SIZE)]
    completed = set(state.get("completed_batches", []))

    if not completed and os.path.exists(output_path):
        os.remove(output_path)          # swiezy run od zera
        state["header_written"] = False

    for index, batch in enumerate(batches):
        if index in completed:
            print(f"Batch {index + 1}/{len(batches)}: pominiety (resume).")
            continue
        last_error: Exception | None = None
        for attempt in range(1 + BATCH_RETRIES):
            try:
                csv_text = fetcher(batch)
                last_error = None
                break
            except BatchError as exc:
                last_error = exc
                print(f"Batch {index + 1}: proba {attempt + 1} padla: {exc}")
        if last_error is not None:
            # FAIL-LOUD (fix cichego `continue` z v1): stan zachowany,
            # rerun wznowi od tego batcha.
            sys.exit(f"ERROR: batch {index + 1}/{len(batches)} nieudany po "
                     f"{1 + BATCH_RETRIES} probach: {last_error}. "
                     f"Stan w {state_path} — uruchom ponownie (z VPN), "
                     f"aby wznowic od tego miejsca.")

        header, data_rows = enrich_with_hash(csv_text, target_map)
        write_batch(output_path, header, data_rows,
                    write_header=not state.get("header_written", False))
        state["header_written"] = True
        state.setdefault("completed_batches", []).append(index)
        save_state(state, state_path)
        print(f"Batch {index + 1}/{len(batches)}: {len(data_rows)} wierszy.")

    if os.path.exists(state_path):
        os.remove(state_path)           # run domkniety — stan skonsumowany
    return len(batches)


def main() -> int:
    if not os.path.exists(INPUT_CSV):
        sys.exit(f"ERROR: brak {INPUT_CSV} (bez input() — decyzja Q2/F7).")
    target_map = load_target_map(INPUT_CSV)
    if not target_map:
        sys.exit("ERROR: zero targetow Domain w wejsciu.")
    session = build_session()
    state = load_state()
    if state.get("completed_batches"):
        print(f"Resume: {len(state['completed_batches'])} batchy ukonczonych.")
    run(target_map, lambda batch: fetch_report_csv(session, batch), state)
    print("--- Zakonczono ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
