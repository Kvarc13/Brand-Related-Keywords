"""
Keyword_Step5__Client_Report_Keywords.py  (Faza 8 — v2)
=======================================================
Cienki entrypoint kroku 5 — JEDYNA warstwa polityk pipeline'u (Q11).
Implementacja w pakiecie `report/`; v1 w legacy/. Wyjscia:
Brandable_Domains.csv (klient), -INTERNAL.csv (pelny audyt z Cut_Reason),
-GROUPED.md (widok Advertisera Brand -> Keyword -> misspellingi).
Similarity_Score = FinalScore (koniec mostu Score_Custom).

Polityka Popular_Domain wymaga przypietej listy Tranco (raz):
  Invoke-WebRequest https://tranco-list.eu/download/XN67N/1000000 `
    -OutFile fixtures/data/tranco_XN67N_top1M.csv
Brak pliku = polityka nieaktywna (glosne ostrzezenie), reszta dziala.
"""

import logging
import sys

from report.pipeline import run_report

CONFIG = {
    "min_visits": 0,                    # prog ruchu dla widoku klienta
    "tranco_file": "fixtures/data/tranco_XN67N_top1M.csv",
    "enable_llm_gate": False,           # opcjonalna recenzja LLM (koszt!)
    "llm_gate_threshold": 0.85,
    "output_client": "Brandable_Domains.csv",
    "output_internal": "Brandable_Domains-INTERNAL.csv",
    "output_grouped": "Brandable_Domains-GROUPED.md",
}

MISSPELLINGS_FILE = "Keyword_Specific_Misspellings.csv"
TARGETDATA_FILE = "TargetData.csv"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    gate = None
    if CONFIG["enable_llm_gate"]:
        from keywordgen.llm import OpenAITransport
        from report.llm_gate import build_gate
        gate = build_gate(OpenAITransport(), CONFIG["llm_gate_threshold"])
    stats = run_report(MISSPELLINGS_FILE, TARGETDATA_FILE, CONFIG, llm_gate=gate)
    return 0 if stats["client_rows"] >= 0 else 2


if __name__ == "__main__":
    sys.exit(main())
