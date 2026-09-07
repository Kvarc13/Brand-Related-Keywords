"""
Faza 4 — walidatory i selekcja (czysta logika, 100% offline-testowalna).

Kolejnosc rurociagu per brand:
  merge_generations -> Candidate(core/low_conf) -> validate_candidates
  (pulapki, ASCII+join, dlugosc, blocklista, dedup) -> inject_sld ->
  [sedzia: flagged] -> priority_fill (Tier1 przed Tier2, cap kategorii).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from common.domains import registrable_sld

MIN_LEN = 4
MAX_LEN = 20
BRAND_TIERS = ("brand_core", "branded")
TIER_ORDER = {"brand_core": 0, "branded": 1, "category_strong": 2}

_ASCII_TOKEN = re.compile(r"^[a-z0-9]+$")


@dataclass
class Candidate:
    keyword: str                 # znormalizowany token (lower, sklejony)
    specificity: str
    type: str
    rationale: str = ""
    confidence: str = "high"     # high = rdzen self-consistency; low = roznica
    judge: str = "not_required"  # not_required | accepted | rejected | error
    rejected_reason: str = ""
    in_context: bool = False


@dataclass
class ValidationReport:
    kept: list[Candidate] = field(default_factory=list)
    dropped: list[Candidate] = field(default_factory=list)

    def drop(self, candidate: Candidate, reason: str) -> None:
        candidate.rejected_reason = reason
        self.dropped.append(candidate)


def load_blocklist(path: Path) -> set[str]:
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            words.add(line)
    return words


def normalize_token(raw: str) -> str:
    """lower + sklejenie spacji/myslnikow ('Fire TV' -> 'firetv') + strip."""
    return re.sub(r"[\s\-_]+", "", str(raw or "").strip().lower())


def merge_generations(generations: list[list[dict]]) -> list[Candidate]:
    """Self-consistency: przeciecie (po tokenie) = confidence high; roznica
    symetryczna = low. Metadane (specificity/type) z pierwszego wystapienia."""
    seen_per_gen: list[set[str]] = []
    first_meta: dict[str, dict] = {}
    order: list[str] = []
    for generation in generations:
        tokens: set[str] = set()
        for item in generation:
            token = normalize_token(item.get("keyword", ""))
            if not token:
                continue
            tokens.add(token)
            if token not in first_meta:
                first_meta[token] = item
                order.append(token)
        seen_per_gen.append(tokens)
    if not seen_per_gen:
        return []
    core = set.intersection(*seen_per_gen) if len(seen_per_gen) > 1 else seen_per_gen[0]
    candidates: list[Candidate] = []
    for token in order:
        meta = first_meta[token]
        candidates.append(Candidate(
            keyword=token,
            specificity=str(meta.get("specificity", "weak")),
            type=str(meta.get("type", "other")),
            rationale=str(meta.get("rationale", ""))[:200],
            confidence="high" if token in core else "low",
        ))
    return candidates


def validate_candidates(candidates: list[Candidate],
                        blocklist: set[str]) -> ValidationReport:
    report = ValidationReport()
    seen: set[str] = set()
    for candidate in candidates:
        token = candidate.keyword
        if candidate.type == "third_party_brand":
            report.drop(candidate, "trap:third_party_brand")
        elif candidate.specificity == "weak":
            report.drop(candidate, "trap:weak")
        elif candidate.specificity not in TIER_ORDER:
            report.drop(candidate, f"unknown_specificity:{candidate.specificity}")
        elif not _ASCII_TOKEN.match(token):
            report.drop(candidate, "non_ascii_or_symbols")
        elif not (MIN_LEN <= len(token) <= MAX_LEN):
            report.drop(candidate, f"length:{len(token)}")
        elif token in blocklist:
            report.drop(candidate, "blocklist")
        elif token in seen:
            report.drop(candidate, "duplicate")
        else:
            seen.add(token)
            report.kept.append(candidate)
    return report


def inject_sld(candidates: list[Candidate], brand_domain: str) -> list[Candidate]:
    """Gwarantowany token brandu (fix hims/gilt): SLD zawsze obecny jako
    brand_core, NIEZALEZNIE od MIN_LEN — polityke dystansu dla krotkich
    stringow przejmuje retrieval Fazy 5."""
    sld = registrable_sld(brand_domain)
    if not sld:
        return candidates
    if any(c.keyword == sld for c in candidates):
        for candidate in candidates:
            if candidate.keyword == sld:
                candidate.specificity = "brand_core"
                candidate.confidence = "high"
        return candidates
    return [Candidate(keyword=sld, specificity="brand_core", type="brand",
                      rationale="injected SLD (gwarancja tokenu brandu)")] + candidates


def mark_in_context(candidates: list[Candidate], context_text: str) -> None:
    lowered = (context_text or "").lower()
    for candidate in candidates:
        candidate.in_context = bool(lowered) and candidate.keyword in lowered


def keywords_needing_judge(candidates: list[Candidate],
                           brand_is_fallback: bool) -> list[dict]:
    """Kto idzie do sedziego: KAZDY category_strong (pytanie CAT) oraz
    low-confidence z tierow brandowych (pytanie NAV). Dla brandow fallback
    sedzia dostaje grounding (Q9) — decyzja o narzedziach w llm/runner."""
    items: list[dict] = []
    for candidate in candidates:
        if candidate.rationale.startswith("injected SLD"):
            continue
        if candidate.specificity == "category_strong":
            items.append({"keyword": candidate.keyword, "question": "CAT"})
        elif candidate.specificity in BRAND_TIERS and candidate.confidence == "low":
            items.append({"keyword": candidate.keyword, "question": "NAV"})
    return items


def apply_judge_verdicts(candidates: list[Candidate],
                         verdicts: dict[str, bool] | None,
                         judged_tokens: set[str]) -> list[Candidate]:
    """verdicts None = sedzia padl -> kandydaci zostaja z judge='error'
    (nie-destrukcyjnie, transparentnie); inaczej fail-closed: brak/False
    dla ocenianego tokenu = odrzut."""
    kept: list[Candidate] = []
    for candidate in candidates:
        if candidate.keyword not in judged_tokens:
            kept.append(candidate)
            continue
        if verdicts is None:
            candidate.judge = "error"
            kept.append(candidate)
        elif verdicts.get(candidate.keyword, False):
            candidate.judge = "accepted"
            kept.append(candidate)
        else:
            candidate.judge = "rejected"
            candidate.rejected_reason = "judge_rejected"
    return kept


def priority_fill(candidates: list[Candidate], max_keywords: int,
                  category_cap_ratio: float = 0.5) -> list[Candidate]:
    """Sloty wypelniane Tier1 przed Tier2; kategoria <= cap (domyslnie 50%
    slotow). Sloty moga zostac puste — nigdy nie przekraczamy capu, brak
    keywordow lapie retry-on-failure w runnerze."""
    category_cap = int(max_keywords * category_cap_ratio)
    ordered = sorted(
        candidates,
        key=lambda c: (TIER_ORDER.get(c.specificity, 9),
                       0 if c.confidence == "high" else 1),
    )
    selected: list[Candidate] = []
    category_count = 0
    for candidate in ordered:
        if len(selected) >= max_keywords:
            break
        if candidate.specificity == "category_strong":
            if category_count >= category_cap:
                continue
            category_count += 1
        selected.append(candidate)
    return selected


def brand_direct_count(candidates: list[Candidate]) -> int:
    """Tier1 poza wstrzyknietym SLD (warunek retry-on-failure)."""
    return sum(1 for c in candidates
               if c.specificity in BRAND_TIERS
               and not c.rationale.startswith("injected SLD"))
