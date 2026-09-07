"""
Faza 2 — kontrakt nazw plikow pipeline'u (jedyne zrodlo prawdy).

Stare skrypty NIE sa przepinane na te stale (kod idzie do przebudowy w
Fazach 3-8; przepinanie = churn bez zmiany zachowania). Fazy przebudowy
importuja stad. Test pinujacy gwarantuje zgodnosc stalych z literalami
uzywanymi dzis przez skrypty — rozjazd kontraktu wykryje pytest, nie klient.
"""

# Wejscia zewnetrzne
BRANDS_CSV = "Brands.csv"
ZEROPARK_DOMAIN_LIST_CSV = "Zeropark_Domain_List.csv"
DOM_TARGETS_CSV = "dom_targets_nokey.csv"

# Krok 1
BRAND_CONTENT_JSON = "brand_content_final.json"
FAILED_BRANDS_CSV = "failed_brands.csv"

# Krok 2
TRANSFORMED_KEYWORDS_CSV = "Transformed_Keywords.csv"
KEYWORD_ERRORS_CSV = "Keyword_Errors.csv"

# Krok 3
KEYWORD_MISSPELLINGS_CSV = "Keyword_Specific_Misspellings.csv"

# Krok 4 / 4.5
TARGET_ID_CSV = "TargetID.csv"
TARGET_DATA_CSV = "TargetData.csv"

# Krok 5
CLIENT_REPORT_CSV = "Brandable_Domains.csv"
INTERNAL_REPORT_CSV = "Brandable_Domains-INTERNAL.csv"
