"""
Faza 2 — jedna, autorytatywna normalizacja domen (tldextract, offline).

Zastepuje trzy niezalezne implementacje o roznej semantyce:
  - Step 1 `normalize_domain`: BUG `replace("www.","")` kaleczyl domeny
    zawierajace 'www.' w srodku ('awww.com' -> 'a.com'); tu: strip wylacznie
    PREFIKSU. (Wpiecie do Step 1 w Fazie 3 — plik idzie do przebudowy.)
  - Step 2 `clean_domain`: urlparse+www-prefix; dla golych domen `x.tld`
    wynik identyczny z `normalize_domain` (przypiete testem — warunek
    zgodnosci kluczy z brand_content_final.json wygenerowanym przez Step 1).
  - Step 3 `strip_tld`: BUG na multi-label TLD ('amazon.co.uk' -> 'amazon.co',
    co wypadalo z prefiltra dlugosci — utrata calej klasy ccTLD-typosquatow);
    tu: registrable_sld przez Public Suffix List.

Determinizm: TLDExtract z suffix_list_urls=() uzywa WYLACZNIE snapshotu PSL
zbundlowanego z pakietem — zero polaczen sieciowych, wynik powtarzalny
miedzy maszynami. Aktualizacja PSL = swiadomy bump wersji tldextract.

PULAPKA (zweryfikowana empirycznie, przypieta testami): gole slowa bedace
jednoczesnie gTLD ('travel', 'hotels', 'fitness', 'app', ...) daja w
tldextract domain='' — naiwne uzycie skasowaloby te keywordy z matchingu.
registrable_sld parsuje przez PSL TYLKO wartosci zawierajace kropke;
goly token (keyword) wraca jako-jest (lower).
"""

from __future__ import annotations

import tldextract

# Snapshot PSL z pakietu; zadnych pobran w runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())


def normalize_domain(raw: object) -> str:
    """Domena w formacie kluczy brand_content_final.json: lower, bez schematu,
    sciezki i wylacznie PREFIKSU 'www.'.

    'https://www.Amazon.com/x' -> 'amazon.com';  'awww.com' -> 'awww.com'.
    """
    value = str(raw).strip().lower()
    if not value or value == "nan":
        return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return value


def registrable_sld(value: object) -> str:
    """SLD domeny rejestrowalnej ('shop.amazon.co.uk' -> 'amazon') lub goly
    token bez zmian ('travel' -> 'travel', bo brak kropki = keyword, nie FQDN).

    Fallback dla patologii czysto-sufiksowych ('co.uk' -> 'co'): pierwszy
    segment, zeby nigdy nie zwrocic '' dla niepustego wejscia.
    """
    text = str(value).strip().lower()
    if not text or text == "nan":
        return ""
    if "." not in text:
        return text
    ext = _extract(text)
    if ext.domain:
        return ext.domain
    return text.split(".", 1)[0]


def address_matching_sld(value: object) -> str:
    """SLD adresu z listy inventory do FUZZY-matchingu (strona adresowa Step 3).

    Zasady (Faza 2.1 — przywrocenie efektywnego zakresu baseline'u):
      - goly token bez kropki -> bez zmian ('amazone' matchowalo w baseline);
      - adres z SUBDOMENA (w tym 'www.') -> '' : subdomeny poza fuzzy
        matchingiem do decyzji Q14. Stary strip_tld wykluczal je de facto
        przez prefiltr dlugosci; po naiwnym fixie flood subdomen (np. 5x
        ww38.*.amaczon.com) wypychal realne typosquaty z heapa top-30 —
        dowod: diff_faza2 (raport 1498 -> 1156, utrata m.in. academy.cm).
      - brak poprawnego publicznego sufiksu ('amazon.com9788072942671') ->
        '' : smieciowy wpis; w starym kodzie byl to FALSE POSITIVE (exact!).
      - inaczej: SLD domeny rejestrowalnej ('amaz0n.co.uk' -> 'amaz0n').

    Pusty wynik => matcher pomija adres (istniejacy guard `if not address_sld`).
    """
    text = str(value).strip().lower()
    if not text or text == "nan":
        return ""
    if "." not in text:
        return text
    ext = _extract(text)
    if not ext.suffix or ext.subdomain:
        return ""
    return ext.domain


def registrable_domain(value: object) -> str:
    """Domena rejestrowalna ('shop.amazon.co.uk' -> 'amazon.co.uk').
    Uzycie od Fazy 5+ (TLD_Match, kanal generacyjny)."""
    text = normalize_domain(value)
    if not text:
        return ""
    ext = _extract(text)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return text
