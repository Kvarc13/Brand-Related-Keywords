"""
Faza 8 — opcjonalna bramka LLM (flaga, default OFF).

Recenzuje wiersze WIDOCZNE dla klienta o niskim score (< prog): jeden
call per brand na wspolnym transporcie z Fazy 4 (keywordgen.llm).
Odrzucenie = Cut_Reason 'llm_gate' (audyt jak kazda polityka).
Blad transportu = wiersze ZOSTAJA (nie-destrukcyjnie) z logiem.
"""

from __future__ import annotations

import logging

import pandas as pd

from keywordgen.models import JudgeResult
from keywordgen.prompts import judge_messages

logger = logging.getLogger("report.llm_gate")

SCORE_THRESHOLD = 0.85


def build_gate(transport, threshold: float = SCORE_THRESHOLD):
    """Zwraca funkcje-bramke df->df dla report.pipeline.run_report."""

    def gate(merged: pd.DataFrame) -> pd.DataFrame:
        merged = merged.copy()
        mask = (merged["Cut_Reason"] == "") & \
               (merged["Similarity_Score"] < threshold)
        for brand, brand_df in merged[mask].groupby("Brand"):
            items = [{"keyword": str(r["Keyword_Misspelling"]), "question": "NAV"}
                     for r in brand_df.to_dict("records")]
            try:
                result, _usage = transport.judge(
                    judge_messages(str(brand), items), grounded=False)
                rejected = {v.keyword for v in result.verdicts if not v.accept}
            except Exception as exc:
                logger.warning("[%s] bramka LLM padla: %s — wiersze zostaja.",
                               brand, str(exc)[:120])
                continue
            reject_mask = mask & (merged["Brand"] == brand) & \
                merged["Keyword_Misspelling"].isin(rejected)
            merged.loc[reject_mask, "Cut_Reason"] = "llm_gate"
        return merged

    return gate
