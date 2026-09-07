"""
Faza 8 — polityki widocznosci raportu klienckiego (Q11).

JEDYNA warstwa polityk w calym pipeline (Step 3 = recall-max + metadane).
Kazde ciecie zostawia slad: wiersz trafia do raportu INTERNAL z Cut_Reason
— polityka bez sladu to zgadywanie, nie polityka.

Macierz (Q11):
  misspelling / generated / affix  -> widoczne dla klienta
  exact_other_tld                  -> widoczne TYLKO dla tierow brandowych
                                      (brand_core/branded); generyk pod obcym
                                      TLD to zwykle cudza, legalna strona
  self                             -> poza widokiem klienta (wlasna domena
                                      brandu to nie brandable misspelling;
                                      w internal jako etykieta)
  Popular_Domain (Tranco, pinned)  -> poza widokiem klienta (popularna,
                                      zywa domena = nie typo-inventory)
  min_visits (config, default 0)   -> ponizej progu poza widokiem klienta
"""

from __future__ import annotations

import logging
from pathlib import Path

from common.domains import registrable_domain

logger = logging.getLogger("report.policies")

BRAND_TIERS = {"brand_core", "branded"}
CLIENT_VISIBLE_TYPES = {"misspelling", "generated", "affix"}


def load_tranco(path: Path) -> set[str]:
    """Przypieta lista Tranco (rank,domain). Brak pliku = GLOSNE ostrzezenie
    i pusty zbior (polityka Popular_Domain nieaktywna, reszta dziala)."""
    if not path.exists():
        logger.warning("Brak listy Tranco (%s) — polityka Popular_Domain "
                       "NIEAKTYWNA. Pobranie: patrz PHASES.md / runbook F8.", path)
        return set()
    domains: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[-1]:
                domains.add(parts[-1].strip().lower())
    logger.info("Tranco: %d domen (pinned).", len(domains))
    return domains


def cut_reason(row: dict, tranco: set[str], min_visits: float) -> str:
    """'' = widoczny dla klienta; inaczej powod ciecia (do Cut_Reason)."""
    match_type = str(row.get("Match_Type", ""))
    specificity = str(row.get("Specificity", ""))

    if match_type == "self":
        return "self_domain"
    if match_type == "exact_other_tld" and specificity not in BRAND_TIERS:
        return "exact_other_tld_non_brand_tier"
    if match_type not in CLIENT_VISIBLE_TYPES | {"exact_other_tld"}:
        return f"unknown_match_type:{match_type}"

    if tranco:
        address = str(row.get("Keyword_Misspelling", ""))
        if registrable_domain(address) in tranco:
            return "popular_domain"

    visits = row.get("Total Visits", 0)
    try:
        visits = float(visits)
    except (TypeError, ValueError):
        visits = 0.0
    if visits < min_visits:
        return "below_min_visits"
    return ""
