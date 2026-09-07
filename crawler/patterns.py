"""
Faza 3 — wzorce detekcji jakosci contentu (dane, nie logika).

Scalone zrodla: listy `js_required` / `bot_protection` / `geo` z projektu
hackathon-scraping (bogatsze o klasy Cloudflare i geo-scian) + stare
BOT_KEYWORDS crawlera v1 (parked/for-sale). Wszystkie dopasowania
case-insensitive na polaczonym tekscie (title + meta + body).
"""

JS_REQUIRED = (
    "enable javascript",
    "javascript is disabled",
    "please enable js",
    "requires javascript",
    "you need to enable javascript",
    "this site requires javascript",
    "enable cookies and javascript",
)

BOT_PROTECTION = (
    "cloudflare",
    "cf-browser-verification",
    "checking your browser",
    "just a moment",
    "attention required",
    "captcha",
    "verify you are human",
    "access denied",
    "robot check",
    "security challenge",
    "blocked",
)

GEO_BLOCK = (
    "not available in your country",
    "not available in your region",
    "not available in your location",
    "access denied based on your location",
    "this content is not available in your location",
    "we're sorry, this service is unavailable in your region",
    "is unavailable in your country",
    "is unavailable in your location",
    "is not available in your country",
    "restricted country",
)

PARKED = (
    "parked domain",
    "this domain is for sale",
    "buy this domain",
    "domain parking",
    "coming soon",
)

# Podstrony tozsamosciowe (kanal opcjonalny Fazy 3) — podzbior listy `web`
# z zipa ograniczony do identity (agenda/schedule to specyfika eventowa,
# poza celem brandowym) + strony produktowe.
SUBPAGE_PATTERNS = (
    r"/about(-us)?/?$",
    r"/aboutus/?$",
    r"/our-story/?$",
    r"/who-we-are/?$",
    r"/company/?$",
    r"/brands?/?$",
    r"/products?/?$",
    r"/collections?/?$",
)
