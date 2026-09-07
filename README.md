# Brandable Domains — Keyword Misspellings Pipeline

Discovers monetizable typo-traffic for brands at scale: given a list of brand
domains, the pipeline crawls brand context, generates navigational-intent
keywords with an LLM, fuzzy-matches them against **14.8M domains** of Zeropark
inventory, resolves target IDs, enriches them with 30-day traffic stats and
produces audited, client-ready reports — end to end, resumable at every stage.

```
Brands.csv ─▶ [1] Crawler ─▶ [2] Keyword Gen (LLM) ─▶ [3] Matcher ─▶ [4] Target IDs ─▶ [4.5] Traffic API ─▶ [5] Reports
```

## What it produces

| File | Audience | Contents |
|---|---|---|
| `Brandable_Domains.csv` | client | policy-filtered rows only, sorted brand-tier → visits |
| `Brandable_Domains-INTERNAL.csv` | internal | **every** joined row + `Cut_Reason` audit column |
| `Brandable_Domains-GROUPED.md` | advertiser | Brand → Keyword → table of domains (visits, cost, score, match type) |

Every row carries full provenance: `Match_Type` (misspelling / affix /
exact_other_tld / self / generated), `Specificity` tier, `FinalScore`,
QWERTY/phonetic signals, `In_Context`, judge status.

## Pipeline stages

1. **Crawler** (`crawler/`) — tiered brand-context acquisition, cheapest
   source first: plain HTTP → Playwright (escalated pages only, resource
   blocking) → Common Crawl via AWS Athena + WARC → Wayback Machine
   (freshness window) → search snippet. Rich schema v2: `site_name`,
   JSON-LD summary, nav links, H1/H2, Wikidata, identity subpages.
   JSONL checkpoint, resume skips resolved domains.
2. **Keyword generation** (`keywordgen/`) — GPT with strict Structured
   Outputs. Two-tier value model: Tier 1 brand-direct (name, owned product
   lines, proprietary tech) > Tier 2 category-auction. Classification traps
   (`third_party_brand`, `weak`) instead of prompt bans; self-consistency
   (n=2); one judge call per brand — **web-search-grounded** for brands
   without crawled context; retry-on-failure with feedback; per-brand
   checkpoint and cost log. Two transports: **Batch API** (−50 % cost,
   24 h window, resumable job state) and **sync** with a per-brand thread
   pool (`workers`, sequential inside a brand, parallel across brands).
3. **Matcher** (`matcher/`) — SymSpell-style delete-index retrieval
   (length-dependent thresholds: len 4–5 → d≤1, len 6+ → d≤2, no
   recall-killing prefilters), frozen *typo-plausibility* scoring
   (Damerau-Levenshtein with QWERTY-neighbour discount and multiplicative
   phonetic forgiveness; pinned to 3 decimals by tests), `Match_Type`
   taxonomy as pure metadata, canonical ordering, **byte-identical output**
   regardless of chunking/process count. Optional dnstwist-style generated
   channel probes permutations in the same streaming pass. Lookup cost is
   independent of keyword count — 14.8M addresses in ~2 min on 12 cores.
4. **Target resolution** (`Keyword_Step4__MatchTargetID.py`) — streams the
   2 GB target dump once (byte-based progress), builds an
   address → `[(id, feed), …]` **multimap** so no id/feed pair is silently
   lost; output deduped by ID.
5. **Traffic + reports** (`Keyword_Step4_5…`, `report/`) — Zeropark Reports
   API behind VPN + SSO (no credentials in code): timeouts everywhere,
   bounded polling, retry-then-fail-loud, per-batch resume state. Step 5 is
   the **only policy layer** in the pipeline: `self` and popular domains
   (pinned Tranco top-1M) cut from the client view, `exact_other_tld` kept
   only for brand tiers, configurable `min_visits`; every cut is recorded
   in the internal report with a reason. Optional LLM review gate behind a
   flag.

## Quickstart

Requirements: Python 3.13, `pip install -r` equivalents:
`openai pydantic python-dotenv requests beautifulsoup4 playwright pandas
tqdm jellyfish metaphone tldextract` (+ `boto3` for Common Crawl,
`ddgs` for the search-snippet channel). `playwright install chromium`.

```bash
# .env (repo root)
OPENAI_API_KEY=...
AWS_PROFILE=...            # Common Crawl / Athena
AWS_REGION=us-east-1
S3_OUTPUT=s3://your-athena-results/prefix/
DATABASE=ccindex
TABLE=ccindex
```

One-time: pinned Tranco list for the popular-domain policy:

```powershell
New-Item -ItemType Directory -Force fixtures\data | Out-Null
Invoke-WebRequest https://tranco-list.eu/download/XN67N/1000000 -OutFile fixtures\data\tranco_XN67N_top1M.csv
```

Run everything (VPN required from step 4.5):

```bash
python tools/run_pipeline.py                       # all steps
python tools/run_pipeline.py --steps 2,3,4,4.5,5   # from keyword gen down
```

A failed step stops the run loudly; **re-running the same command resumes**
(crawl/keywordgen checkpoints, Batch job state, per-batch traffic state).
For a truly fresh run delete `crawl_checkpoint.jsonl`,
`keywordgen_checkpoint.jsonl`, `batch_state.json`, `step45_state.json`.

Reference snapshots (frozen outputs + timings for later regression diffs):

```bash
python tools/golden_run.py --steps 1,2,3,4,4.5,5 --tag my_run
python tools/golden_diff.py --baseline fixtures/golden/my_run --candidate . --out diff_dir
```

## Quality instrumentation

Human-labelled eval set (`fixtures/eval/keyword_eval.csv`) measures every
generation independently of the pipeline:

```bash
python tools/eval_score.py --keywords Transformed_Keywords.csv --per-vertical --gate 0.83
python tools/eval_build.py --update --keywords Transformed_Keywords.csv --sample-per-vertical 5
```

`--gate` is the post-run ritual (warn by default, `--gate-mode fail` for
CI); `--per-vertical` shows which industries the model handles well;
`--sample-per-vertical` appends a deterministic (seed=42) labelling sample
without touching existing labels. Measured on the proof runs: keyword
precision **0.98** on labelled pairs with **60 %** brand-direct share
(pre-system baseline: 0.83 / 19 %).

## Configuration

Each entry script exposes a `CONFIG` dict at the top — the operational
surface (no CLI flags to memorize): concurrency (`http_concurrency`,
`ws_concurrency`, keywordgen `workers`), model + `reasoning_effort`,
`self_consistency_n`, `max_keywords` + category cap, sync/batch/auto mode
with `batch_threshold`, `min_visits`, Tranco path, optional channels
(subpages / Wikidata / search snippet / generated matcher channel / LLM
report gate).

## Testing

```bash
python -m pytest tests/ -q     # 126 tests
```

The suite pins frozen decisions rather than implementation details:
scoring vectors to 3 decimals, recall sets (`qmazon`, `amazon.co.uk`,
`kindel`…), the Q11 policy matrix, cleaning rules, checkpoint/resume
semantics, bounded polling, byte-identical determinism under
multiprocessing, judge fail-open/fail-closed behaviour, and the audit
trail (`dropped` reasons incl. judge rejections). All LLM logic is tested
offline against an injectable fake transport.

## Project layout

```
common/        domain normalization (tldextract, offline PSL), file contracts
crawler/       schema v2 parser, quality gate, sources (http/browser/CC/wayback), enrich, orchestrator
keywordgen/    models (pydantic + strict schemas), prompts, validators, LLM transport, sync runner, batch mode
matcher/       symspell index, frozen scoring, generated channel, streaming pipeline
report/        Q11 policies, exports, optional LLM gate
tools/         run_pipeline, golden_run/golden_diff, eval_build/eval_score, crawl_report
tests/         126 tests (all offline)
fixtures/      golden snapshots, human-labelled eval set, pinned Tranco list
Keyword_Step*.py   thin entrypoints (the operational contract)
```

## Design principles

- **Collect wide, decide once**: gathering stages maximize recall and
  label everything; all visibility decisions live in a single, audited
  policy layer at the end.
- **Traps over bans**: the LLM classifies honestly (`third_party_brand`,
  `weak`) and validators cut with recorded reasons — nothing disappears
  silently, including judge rejections.
- **Determinism is non-negotiable**: identical inputs produce identical
  bytes, regardless of thread/process scheduling.
- **Resume everywhere**: at thousands of brands, a crash at 80 % must
  never cost a restart from zero — or a second payment.
- **Frozen decisions are frozen**: scoring formulas, cleaning rules and
  policy semantics are pinned by tests and change only with new evidence.
