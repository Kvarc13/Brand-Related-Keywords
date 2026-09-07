"""
Faza 8 — pipeline raportu: walidacja -> join -> dedup per-brand (Q10) ->
polityki (Q11, z auditem Cut_Reason) -> trzy eksporty.

Eksporty:
  Brandable_Domains.csv           klient, plaski, TYLKO wiersze widoczne;
                                  Similarity_Score = FinalScore (koniec
                                  mostu Score_Custom); sort tier -> visits
  Brandable_Domains-INTERNAL.csv  WSZYSTKIE wiersze + Cut_Reason + feed hash
                                  + kolumny F4 (pelny audyt polityk)
  Brandable_Domains-GROUPED.md    widok Advertisera: Brand -> Keyword
                                  (specificity) -> [misspelling, visits,
                                  cost, score, match_type, tld, in_context]

Dedup per-brand (Q10, zmiana vs v1-global): ten sam misspelling pod dwoma
keywordami JEDNEGO brandu -> zostaje najlepszy (porzadek kanoniczny);
pod ROZNYMI brandami -> zostaje u kazdego (to rozne oferty).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from report import policies

logger = logging.getLogger("report")

TIER_ORDER = {"brand_core": 0, "branded": 1, "category_strong": 2}

MISSPELLING_REQUIRED = ["Brand", "Keyword", "Keyword_Misspelling", "Specificity",
                        "Match_Type", "FinalScore", "Distance",
                        "Score_JaroWinkler", "TLD_Match", "In_Context",
                        "Context_Used"]
TARGETDATA_REQUIRED = ["Target Address", "Target Hash", "Total Visits",
                       "Avg. Cost", "Publisher Feed Hash"]

CLIENT_COLUMNS = ["Brand", "Keyword", "Specificity", "Keyword_Misspelling",
                  "Match_Type", "TLD_Match", "In_Context", "Total Visits",
                  "Avg. Cost", "Similarity_Score", "Target Hash"]


def validate_columns(df: pd.DataFrame, required: list[str], name: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"ERROR: {name} bez kolumn {missing}. "
                         f"Sa: {list(df.columns)}")


def join_and_dedup(misspellings: pd.DataFrame,
                   targetdata: pd.DataFrame) -> pd.DataFrame:
    misspellings = misspellings.copy()
    targetdata = targetdata.copy()
    misspellings["_key"] = misspellings["Keyword_Misspelling"].astype(str) \
        .str.strip().str.lower()
    targetdata["_key"] = targetdata["Target Address"].astype(str) \
        .str.strip().str.lower()

    # Q10: dedup PER-BRAND w porzadku kanonicznym (final v -> d ^ -> JW v)
    misspellings.sort_values(
        by=["FinalScore", "Distance", "Score_JaroWinkler", "_key"],
        ascending=[False, True, False, True], inplace=True)
    misspellings = misspellings.drop_duplicates(subset=["Brand", "_key"],
                                                keep="first")

    # Guard: kolizja nazw kolumn = ciche sufiksy _x/_y = korupcja — glosno.
    overlap = (set(targetdata.columns) & set(misspellings.columns)) - {"_key"}
    if overlap:
        raise SystemExit(f"ERROR: kolizja kolumn misspellings/TargetData: "
                         f"{sorted(overlap)} — zmien nazwy przed joinem.")
    merged = targetdata.merge(misspellings, on="_key", how="inner")
    merged["Similarity_Score"] = merged["FinalScore"]
    return merged


def apply_policies(merged: pd.DataFrame, tranco: set[str],
                   min_visits: float) -> pd.DataFrame:
    merged = merged.copy()
    merged["Cut_Reason"] = [
        policies.cut_reason(row, tranco, min_visits)
        for row in merged.to_dict("records")
    ]
    return merged


def sort_for_client(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_tier"] = df["Specificity"].map(TIER_ORDER).fillna(9)
    df["_visits"] = pd.to_numeric(df["Total Visits"], errors="coerce").fillna(0)
    df.sort_values(by=["Brand", "_tier", "_visits", "Keyword_Misspelling"],
                   ascending=[True, True, False, True], inplace=True)
    return df.drop(columns=["_tier", "_visits"])


def write_grouped(df: pd.DataFrame, path: Path) -> None:
    """Widok Advertisera: Brand -> Keyword (specificity) -> wiersze."""
    lines: list[str] = ["# Brandable Domains — widok per brand", ""]
    for brand, brand_df in df.groupby("Brand", sort=True):
        lines.append(f"## {brand}")
        for (keyword, specificity), kw_df in brand_df.groupby(
                ["Keyword", "Specificity"], sort=False):
            lines.append(f"### {keyword}  `{specificity}`")
            lines.append("| misspelling | visits | avg. cost | score "
                         "| match | tld | in ctx |")
            lines.append("|---|---|---|---|---|---|---|")
            for row in kw_df.to_dict("records"):
                lines.append(
                    f"| {row['Keyword_Misspelling']} | {row['Total Visits']} "
                    f"| {row['Avg. Cost']} | {row['Similarity_Score']} "
                    f"| {row['Match_Type']} | {row['TLD_Match']} "
                    f"| {row['In_Context']} |")
            lines.append("")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_report(misspellings_file: str, targetdata_file: str, config: dict,
               llm_gate=None) -> dict:
    for path in (misspellings_file, targetdata_file):
        if not Path(path).exists():
            raise SystemExit(f"ERROR: brak {path}")

    misspellings = pd.read_csv(misspellings_file, encoding="utf-8-sig")
    targetdata = pd.read_csv(targetdata_file, encoding="utf-8-sig")
    validate_columns(misspellings, MISSPELLING_REQUIRED, misspellings_file)
    validate_columns(targetdata, TARGETDATA_REQUIRED, targetdata_file)
    logger.info("Wejscie: %d misspellingow, %d wierszy TargetData",
                len(misspellings), len(targetdata))

    tranco = policies.load_tranco(Path(config.get(
        "tranco_file", "fixtures/data/tranco_XN67N_top1M.csv")))
    merged = join_and_dedup(misspellings, targetdata)
    merged = apply_policies(merged, tranco, config.get("min_visits", 0))

    # --- opcjonalna bramka LLM (flaga; identycznosc gdy wylaczona) ---
    if config.get("enable_llm_gate", False) and llm_gate is not None:
        merged = llm_gate(merged)

    merged = sort_for_client(merged)
    client = merged[merged["Cut_Reason"] == ""]

    internal_columns = CLIENT_COLUMNS + [
        c for c in ("Vertical", "Confidence", "Judge", "Context_Used",
                    "Generated_Class", "Distance", "Publisher Feed Hash",
                    "Cut_Reason")
        if c in merged.columns]
    client_path = Path(config.get("output_client", "Brandable_Domains.csv"))
    internal_path = Path(config.get("output_internal",
                                    "Brandable_Domains-INTERNAL.csv"))
    grouped_path = Path(config.get("output_grouped",
                                   "Brandable_Domains-GROUPED.md"))

    client[CLIENT_COLUMNS].to_csv(client_path, index=False)
    merged[internal_columns].to_csv(internal_path, index=False)
    write_grouped(client, grouped_path)

    # --- podsumowanie BEZ zrzutu raportu (fix v1) ---
    cut_stats = merged["Cut_Reason"].value_counts().to_dict()
    cut_stats.pop("", None)
    stats = {"joined": len(merged), "client_rows": len(client),
             "cut": cut_stats, "brands": int(client["Brand"].nunique())
             if len(client) else 0}
    logger.info("Raport: %d wierszy po joinie -> %d dla klienta "
                "(%d brandow); ciecia: %s -> %s / %s / %s",
                stats["joined"], stats["client_rows"], stats["brands"],
                cut_stats, client_path, internal_path, grouped_path)
    return stats
