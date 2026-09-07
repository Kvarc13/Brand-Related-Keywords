"""
Faza 4 — prompty (popyt nawigacyjny) + budowa bloku kontekstu ze schema v2.

Zmiana ramy vs v1: persona 'SEO expert' primowala terminy wyszukiwarkowe
(kategorie), ktorych misspellingi to nie ruch brandu. Nowa rama: analityk
popytu NAWIGACYJNEGO wokol brandu, z jawnie opisanym przeznaczeniem
downstream i dwupoziomowym modelem wartosci (Tier 1 brand-direct >
Tier 2 kategoria-aukcyjna). Blocklista zyje w pliku danych i walidatorach,
NIE w promptcie (zakazy wyliczankowe przeciekaja) — prompt niesie tylko
klasyfikacje z pulapkami.
"""

from __future__ import annotations

GENERATOR_SYSTEM = """You are a navigational-demand analyst for a domain-traffic marketplace.

PURPOSE (downstream): each keyword you output will be fuzzy-matched against
millions of parked/typo domains; misspellings of your keywords become ad
inventory bought for THIS brand. A keyword is valuable only if a user typing
it (or its typo) into a browser/search bar plausibly intends to reach:
 - Tier 1 (best): THIS brand specifically — brand name, owned product lines,
   proprietary technologies, flagship services. specificity=brand_core (the
   brand identity itself) or branded (owned products/tech).
 - Tier 2 (acceptable, subordinate): the CATEGORY this brand actively competes
   in, with purchase/navigation intent (e.g. 'creditcard' for a card issuer).
   specificity=category_strong. Auction traffic — never crowds out Tier 1.

CLASSIFY honestly, do not self-censor the list:
 - a keyword that is another company's brand -> type=third_party_brand
   (e.g. brands merely SOLD by a retailer; private label = branded, OK)
 - descriptive vocabulary without type-in intent (adjectives, marketing
   words, abstract nouns) -> specificity=weak

RULES: every keyword is ONE token: ASCII letters/digits only, no spaces or
hyphens — join multi-word names (Fire TV -> FireTV, American Express ->
AmericanExpress). English output. 6-10 items. brand_name = official name."""

FEWSHOT_CONTEXT_USER = """Brand: examplebrew.com
--- CONTEXT ---
site_name: Example Brew Co
title: Example Brew Co | Craft Nitro Cold Brew
nav: Nitro Cans, BrewPods, Subscriptions, Our Story
json_ld: names: Example Brew Co; desc: Craft nitro cold brew and BrewPods
content: Example Brew makes NitroCan cold brew and the patented BrewPod system..."""

FEWSHOT_CONTEXT_ASSISTANT = """{"brand_name": "Example Brew Co", "vertical": "beverage DTC", "keywords": [
 {"keyword": "examplebrew", "type": "brand", "specificity": "brand_core", "rationale": "typing the brand goes to the brand"},
 {"keyword": "brewpod", "type": "product", "specificity": "branded", "rationale": "patented owned product line"},
 {"keyword": "nitrocan", "type": "product", "specificity": "branded", "rationale": "owned product name"},
 {"keyword": "coldbrew", "type": "category", "specificity": "category_strong", "rationale": "category the brand competes in with purchase intent"},
 {"keyword": "artisanal", "type": "other", "specificity": "weak", "rationale": "descriptive, no type-in intent"}]}"""

FEWSHOT_FALLBACK_USER = """Brand: unknownshoes.example
--- NO CONTEXT AVAILABLE ---
Use only general knowledge. If you do not reliably know this brand, output
the brand token plus honest category_strong keywords for its evident vertical;
do NOT invent product names."""

FEWSHOT_FALLBACK_ASSISTANT = """{"brand_name": "UnknownShoes", "vertical": "footwear", "keywords": [
 {"keyword": "unknownshoes", "type": "brand", "specificity": "brand_core", "rationale": "brand token from the domain"},
 {"keyword": "sneakers", "type": "category", "specificity": "category_strong", "rationale": "evident vertical, purchase intent"},
 {"keyword": "footwear", "type": "category", "specificity": "category_strong", "rationale": "evident vertical"}]}"""

JUDGE_SYSTEM = """You are a strict reviewer of keywords for a domain-traffic marketplace.
For each keyword answer accept=true/false:
 - question NAV (tier brand): would a user typing this term naturally intend
   to land on {brand}'s site specifically?
 - question CAT (tier category): is this a real category {brand} actively
   competes in, with purchase/navigation intent (not descriptive vocabulary)?
Be conservative: reject vague, descriptive, or another company's brand.
Use web search when available to verify unfamiliar product names."""


def build_context_block(record: dict, max_content: int = 2500) -> str:
    """Sklada blok kontekstu z rekordu schema v2 (Faza 3). Puste pola pomijane.
    To tu konsumowany jest zysk Fazy 3: v1 podawal modelowi 500 znakow."""
    parts: list[str] = []

    def add(label: str, value) -> None:
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v)
        value = str(value or "").strip()
        if value:
            parts.append(f"{label}: {value}")

    add("site_name", record.get("site_name"))
    add("title", record.get("title"))
    add("meta_description", record.get("meta_description"))
    add("meta_keywords", record.get("meta_keywords"))
    add("lang", record.get("lang"))
    add("h1", record.get("h1_headings"))
    add("h2", record.get("h2_headings"))
    add("nav", record.get("nav_links"))
    add("json_ld", record.get("json_ld_summary"))
    wikidata = record.get("wikidata") or {}
    if wikidata:
        add("wikidata", f"{wikidata.get('name', '')} | {wikidata.get('description', '')} "
                        f"| aliases: {', '.join(wikidata.get('aliases', []))}")
    for subpage in (record.get("subpages") or [])[:2]:
        add(f"subpage[{subpage.get('title', '')}]",
            (subpage.get("text") or "")[:600])
    add("content", (record.get("main_content") or "")[:max_content])
    return "\n".join(parts)


def generation_messages(brand: str, context_block: str | None) -> list[dict]:
    messages = [
        {"role": "developer", "content": GENERATOR_SYSTEM},
        {"role": "user", "content": FEWSHOT_CONTEXT_USER},
        {"role": "assistant", "content": FEWSHOT_CONTEXT_ASSISTANT},
        {"role": "user", "content": FEWSHOT_FALLBACK_USER},
        {"role": "assistant", "content": FEWSHOT_FALLBACK_ASSISTANT},
    ]
    if context_block:
        messages.append({"role": "user",
                         "content": f"Brand: {brand}\n--- CONTEXT ---\n{context_block}"})
    else:
        messages.append({"role": "user",
                         "content": f"Brand: {brand}\n--- NO CONTEXT AVAILABLE ---\n"
                                    "Use only general knowledge; do NOT invent product names."})
    return messages


def judge_messages(brand: str, items: list[dict]) -> list[dict]:
    """items: [{'keyword':..., 'question': 'NAV'|'CAT'}] — jeden call per brand."""
    listing = "\n".join(f"- {i['keyword']} [{i['question']}]" for i in items)
    return [
        {"role": "developer", "content": JUDGE_SYSTEM.replace("{brand}", brand)},
        {"role": "user",
         "content": f"Brand: {brand}\nReview these keywords and return a verdict "
                    f"for EVERY one of them:\n{listing}"},
    ]
