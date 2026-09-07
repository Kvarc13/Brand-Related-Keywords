"""
Faza 3 — JEDNA bramka jakosci (konsolidacja dwoch rozbieznych z v1).

`classify` zwraca powod odrzucenia albo 'ok' — powod trafia do statystyk
eskalacji i do pola error, wiec wiadomo DLACZEGO domena spadla nizej
w pipeline (v1 mowilo tylko 'flagged'). Geo-sciana wypycha do zrodel
archiwalnych (Common Crawl/Wayback) zamiast byc akceptowana jako content.
"""

from __future__ import annotations

from crawler import patterns

MIN_CONTENT_LENGTH = 100

# Statusy HTTP, ktore od razu kwalifikuja do eskalacji na przegladarke.
ESCALATE_STATUS_CODES = {403, 406, 409, 429, 500, 502, 503, 520}

_CLASS_PATTERNS = (
    ("bot_protection", patterns.BOT_PROTECTION),
    ("js_required", patterns.JS_REQUIRED),
    ("geo_block", patterns.GEO_BLOCK),
    ("parked", patterns.PARKED),
)


def combined_text(result: dict) -> str:
    return " ".join([
        result.get("title", "") or "",
        result.get("meta_description", "") or "",
        result.get("main_content", "") or "",
    ])


def classify(text: str, min_length: int = MIN_CONTENT_LENGTH) -> str:
    """'ok' albo powod: bot_protection | js_required | geo_block | parked | thin."""
    lower = (text or "").lower()
    for reason, needles in _CLASS_PATTERNS:
        if any(needle in lower for needle in needles):
            return reason
    if len(lower.strip()) < min_length:
        return "thin"
    return "ok"


def is_result_good(result: dict, min_length: int = MIN_CONTENT_LENGTH) -> bool:
    if result.get("status") != "success":
        return False
    return classify(combined_text(result), min_length) == "ok"


def gate_reason(result: dict, min_length: int = MIN_CONTENT_LENGTH) -> str:
    """Powod odrzucenia dla statystyk ('ok' gdy rekord dobry)."""
    if result.get("status") != "success":
        return f"status:{result.get('status')}"
    return classify(combined_text(result), min_length)


def should_escalate(status_code: int | None, html: str | None) -> bool:
    """Decyzja tieru HTTP: czy strona wymaga przegladarki (wzorzec z zipa)."""
    if status_code is None or status_code in ESCALATE_STATUS_CODES:
        return True
    if not html or len(html.strip()) < 300:
        return True
    lower = html.lower()
    if any(n in lower for n in patterns.JS_REQUIRED):
        return True
    if any(n in lower for n in patterns.BOT_PROTECTION):
        return True
    return False
