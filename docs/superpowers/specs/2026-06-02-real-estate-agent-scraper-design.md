# Real-Estate Agent Scraper — Design Spec

**Date:** 2026-06-02
**Status:** Approved (user delegated remaining decisions)

## Goal

Dockerized, Scrapling-first, auditable pipeline that collects **publicly available** real-estate-agent contact data for **NJ, PA, IN** and exports it in the exact format of `5 States (Real Estate Agents).xlsx`. Existing rows in that file are the quality bar; the goal is to generate **new/additional** agents in the same format.

## Output contract (canonical)

One sheet, **9 columns, in this order**:

`Agent Full Name | Agent First Name | Agent Last Name | Agent Email | Agent Phone 1 | Agent Phone 2 | Agency Name | City | State`

- **Email is mandatory** on every row.
- Phone format observed: `NNN-NNN-NNNN`. Phone 2 optional.
- No license/address/county/zip/confidence/source columns in the Excel itself.

## Compliance boundary (firm)

- **No bypass.** No CAPTCHA solving, no Cloudflare/Turnstile defeat, no fingerprint spoofing, no login/paywall circumvention. Scrapling runs with **stealth OFF**.
- Respect robots.txt + per-domain rate limits + site ToS.
- Every candidate source passes a **compliance gate** (robots + ToS/anti-bot check) before any fetch.
- High-yield sources that would require bypass are **skipped and documented** in `docs/SOURCES.md` ("Compliant + document the gaps").

## Tooling decision (per source type)

- **httpx + selectolax** — static brokerage roster pages (default, fast).
- **Scrapling (stealth OFF)** — adaptive/self-healing selectors + parsing for shifting HTML.
- **Playwright (vanilla)** — only JS-rendered rosters where robots/ToS permit.
- **Skip + log** — anything requiring circumvention.

## Source discovery

1. **Seed from the existing file** — extract unique agency names, resolve to public sites, scrape NJ/PA/IN office rosters.
2. **National franchise locators** — public office/agent-finder pages, filtered to NJ/PA/IN.
3. **Permissible public directories / associations** — each vetted for robots/ToS.

## Pipeline (collect rich, export narrow)

Internal record (SQLite + JSONL) keeps the 9 fields **plus** `source_url`, `source_name`, `extracted_at`, `confidence`, `license_number?`, `raw_snapshot_hash`, `notes`. Excel export writes **exactly the 9 template columns**.

Stages (resumable via SQLite run-state):
1. `inspect-template` → schema + `config/field_mapping.yaml`.
2. `discover-sources` → `config/sources.yaml` (compliance-gated).
3. `crawl` → fetch rosters, save raw snapshots, emit candidates.
4. `normalize` → name split, phone `NNN-NNN-NNNN`, email validate/lowercase, agency/city/state cleanup.
5. `validate + score` → required-field checks (email mandatory); uncertain → `needs_review` (never dropped).
6. `dedupe + merge` → deterministic ID `state|email|normalized-name`; union provenance.
7. `export` → Excel(9-col) + CSV + JSONL + SQLite + needs_review + summary.
8. `report` → `docs/QUALITY_REPORT.md` + `data/exports/summary_<ts>.csv`.

## Confidence scoring (email-centric)

- **0.90+** — email on brokerage's own domain + name/agency/city present.
- **0.75–0.89** — complete record but generic email or single-source.
- **<0.75** — missing phone / ambiguous name / weak source → `needs_review`.
- Optional `+license corroboration` bump when a state registry confirms name+agency.

## Storage, dedup, resume

- **SQLite** source of truth: `sources`, `raw_snapshots`, `candidates`, `agents`, `run_state`.
- Raw snapshots on disk: `data/raw/<source>/<hash>.html`.
- Resume: per-source cursor + status; re-runs skip completed pages.

## Docker (all software in-container)

- `Dockerfile` — Python 3.10 slim; deps; optional `playwright install chromium` at build.
- `docker-compose.yml` — `scraper` service; mounts `./data`, `./runs`; reads `.env`; CLI entrypoint.
- No secrets unless a permitted official API requires a key.

## Testing (mocked, no live deps)

Unit tests: template reading, field mapping, phone/email normalization, dedup, Excel export shape (9 cols/order), source-interface contract. HTML fixtures for extractors.

## CLI

`python -m src.main {inspect-template | discover-sources | run | export | report}` with `--states NJ PA IN`, `--limit N`, `--quality-first`.

## Deliberate deviations from the brief (Excel contract drives both)

1. Excel exports **9 columns only**; richer data lives in SQLite/JSONL side outputs.
2. Confidence scale is **email-centric** rather than license-centric.

## Known limitation

"Thousands per state in one session" is bounded by compliant rate limits + source fragmentation. System is fully **resumable** so it can accumulate toward that target across runs.
