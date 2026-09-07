"""
Keyword_Step3_MisspelingMatcher.py  (Faza 5 — v2)
=================================================
Kod w pakiecie `matcher/`:
retrieval SymSpell (indeks delecji, progi zalezne od dlugosci: len 4-5
-> d<=1, len 6+ -> d<=2, ZERO prefiltrow pierwszego znaku i +-2 dlugosci),
scoring B "typo-plausibility"
Match_Type (self/exact_other_tld/affix/misspelling — Tylko metadane, nie filtruje wynikow)

"""

import logging
import sys

from matcher.pipeline import run_matcher

CONFIG = {
    "brands_file": "Transformed_Keywords.csv",
    "misspellings_file": "Zeropark_Domain_List.csv",
    "output_file": "Keyword_Specific_Misspellings.csv",
    "chunksize": 200_000,
    "top_n_per_pair": 0,   # 0 = bez capu (recall-max); >0 przywraca limit
    "processes": None,     # None = cpu_count() Bierze wszytkie dostepne rdzenie, >0 = liczba procesow
    "enable_generated_channel": True,   # F6: kanal dnstwist (probe w tym samym skanie)
}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    stats = run_matcher(
        brands_file=CONFIG["brands_file"],
        misspellings_file=CONFIG["misspellings_file"],
        output_file=CONFIG["output_file"],
        chunksize=CONFIG["chunksize"],
        top_n_per_pair=CONFIG["top_n_per_pair"],
        processes=CONFIG["processes"],
        enable_generated=CONFIG["enable_generated_channel"],
    )
    logging.info("Step 3: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
