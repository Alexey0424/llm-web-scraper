# Real-Estate Agent Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Dockerized, Scrapling-first (stealth OFF), resumable pipeline that scrapes publicly-available real-estate-agent contact data for NJ/PA/IN and exports it in the exact 9-column format of `5 States (Real Estate Agents).xlsx`.

**Architecture:** Plug-in source extractors feed normalized candidate records into a SQLite-backed pipeline (crawl → normalize → validate/score → dedupe/merge → export/report). Internal records are rich (provenance, confidence); the Excel export is exactly the 9 template columns. Compliance gate (robots/ToS) precedes every fetch; no bypass of CAPTCHA/anti-bot/login.

**Tech Stack:** Python 3.10, httpx + selectolax, Scrapling (stealth off), Playwright (vanilla, JS-only sources), pandas/openpyxl, pydantic, PyYAML, SQLite, pytest, Docker/compose.

---

## File Structure

```
real_estate_agent_scraper/  (repo root = project dir)
  Dockerfile, docker-compose.yml, requirements.txt, .env.example, README.md, pytest.ini
  config/      states.yaml, sources.yaml, field_mapping.yaml, rate_limits.yaml
  docs/        PLAN.md, TOOLING_DECISION.md, SOURCES.md, DATA_DICTIONARY.md, RUNBOOK.md, QUALITY_REPORT.md
  src/         main.py, cli.py, config.py, logging_setup.py, models.py
    sources/     base.py, registry.py, brokerage_sources.py, directory_sources.py
    extractors/  base.py, html_extractors.py, json_extractors.py
    normalizers/ agent_normalizer.py, phone_email_normalizer.py
    validators/  schema_validator.py, quality_validator.py, duplicate_detector.py
    exporters/   excel_exporter.py, csv_exporter.py, jsonl_exporter.py
    storage/     sqlite_store.py, raw_snapshot_store.py
    utils/       robots.py, rate_limit.py, retry.py, hashing.py, provenance.py, compliance.py
  tests/       test_template_mapping.py, test_normalization.py, test_deduplication.py,
               test_export_excel.py, test_source_contract.py, test_compliance.py, fixtures/
  data/        raw/ processed/ exports/ cache/   runs/
```

## Conventions

- TDD: failing test → minimal impl → green → commit. (No git repo present; commits optional — group by task.)
- All models are pydantic. The canonical in-memory record is `AgentRecord` (rich); export maps to the 9 columns.
- Tests never touch the network — HTML/JSON fixtures only.

---

### Task 1: Project skeleton + models + config

**Files:** Create `requirements.txt`, `src/__init__.py`, `src/models.py`, `src/config.py`, `config/*.yaml`, `pytest.ini`.

- [ ] Define `AgentRecord` pydantic model: the 9 export fields (`full_name, first_name, last_name, email, phone1, phone2, agency, city, state`) plus `source_url, source_name, extracted_at, confidence, license_number, raw_snapshot_hash, notes`. Add `EXPORT_COLUMNS` ordered list mapping field→template header.
- [ ] `config.py` loads YAML (`states`, `sources`, `rate_limits`) + `.env` via os.environ.
- [ ] `config/states.yaml` = NJ/PA/IN. `config/rate_limits.yaml` = default per-domain delay 2s, max 1 concurrent/domain. `config/field_mapping.yaml` = the 9 header→field map.
- [ ] Test `test_template_mapping.py::test_export_columns_match_template` reads the real xlsx header row and asserts `EXPORT_COLUMNS` headers == template headers in order.

### Task 2: Storage layer (SQLite + raw snapshots)

**Files:** Create `src/storage/sqlite_store.py`, `src/storage/raw_snapshot_store.py`, `src/utils/hashing.py`.

- [ ] `hashing.py`: `deterministic_id(state,email,full_name) -> sha1 hex`; `content_hash(bytes)->sha1`.
- [ ] `sqlite_store.py`: schema `sources, raw_snapshots, candidates, agents, run_state`; methods `upsert_candidate`, `iter_candidates`, `upsert_agent`, `iter_agents`, `mark_source_done/get_cursor` (resume), `record_run`.
- [ ] `raw_snapshot_store.py`: `save(source, content)->hash` writes `data/raw/<source>/<hash>.html`; idempotent.
- [ ] Tests: insert→read round-trip; dedup id stable; resume cursor persists across store re-open (temp sqlite).

### Task 3: Compliance + fetch utilities

**Files:** Create `src/utils/robots.py`, `src/utils/rate_limit.py`, `src/utils/retry.py`, `src/utils/compliance.py`.

- [ ] `robots.py`: fetch + cache robots.txt, `is_allowed(url, ua)` using urllib.robotparser.
- [ ] `rate_limit.py`: per-domain token/delay limiter (asyncio-safe + sync).
- [ ] `retry.py`: exponential backoff decorator (max retries, jitter-free deterministic for tests).
- [ ] `compliance.py`: `gate(source)` → returns allow/skip + reason; **hard rule**: any source flagged `requires_bypass: true` in sources.yaml is skipped with reason logged. No stealth, no captcha solving anywhere in codebase.
- [ ] Tests `test_compliance.py`: robots disallow → blocked; `requires_bypass` source → skipped with reason; allowed source → passes.

### Task 4: Source interface + registry

**Files:** Create `src/sources/base.py`, `src/sources/registry.py`, `src/extractors/base.py`.

- [ ] `base.py`: abstract `Source` with `name`, `states`, `discover_pages()`, `fetch(page)->raw`, `extract(raw)->list[AgentRecord]`; must run fetch through compliance gate + rate limiter.
- [ ] `registry.py`: decorator `@register` + `get_sources(states)`.
- [ ] Test `test_source_contract.py`: a dummy registered source yields `AgentRecord`s with required fields populated; fetch refuses a `requires_bypass` source.

### Task 5: Normalizers

**Files:** Create `src/normalizers/phone_email_normalizer.py`, `src/normalizers/agent_normalizer.py`.

- [ ] `normalize_phone(s)->'NNN-NNN-NNNN'|None` (strip, drop +1, format; reject if not 10 digits).
- [ ] `normalize_email(s)->lower/strip|None`, with validity regex.
- [ ] `split_name(full)->(first,last)`; `normalize_state(s)->2-letter`; `clean_agency/city(s)`.
- [ ] Tests `test_normalization.py`: `(317) 796-2689`→`317-796-2689`; `+13177962689`→`317-796-2689`; bad→None; email upper→lower; `New Jersey`→`NJ`.

### Task 6: Validators, scoring, dedup

**Files:** Create `src/validators/schema_validator.py`, `src/validators/quality_validator.py`, `src/validators/duplicate_detector.py`.

- [ ] `schema_validator`: required = email + full_name + state; missing → reason string.
- [ ] `quality_validator.score(rec)`: 0.90+ brokerage-domain email & complete; 0.75–0.89 generic email/single-source; <0.75 → needs_review. Returns (score, bucket).
- [ ] `duplicate_detector.dedupe(records)`: group by `deterministic_id`, merge (union phones, keep best email, union provenance sources, max confidence).
- [ ] Tests `test_deduplication.py`: two records same id merge into one with both source_urls; different ids stay separate; needs_review threshold.

### Task 7: Extractors (HTML + JSON)

**Files:** Create `src/extractors/html_extractors.py`, `src/extractors/json_extractors.py`, fixtures in `tests/fixtures/`.

- [ ] `html_extractors`: selectolax-based generic roster extractor driven by a CSS-selector config (card selector + field subselectors); Scrapling adaptive fallback hook (stealth off).
- [ ] `json_extractors`: parse embedded JSON-LD / `__NEXT_DATA__` person objects.
- [ ] Add fixtures: `brokerage_static.html` (roster cards), `brokerage_jsonld.html`. Tests assert correct field extraction → `AgentRecord`s.

### Task 8: Concrete sources (brokerage + directory)

**Files:** Create `src/sources/brokerage_sources.py`, `src/sources/directory_sources.py`; populate `config/sources.yaml`.

- [ ] `config/sources.yaml`: seed entries for franchise roster sites observed in the data (Coldwell Banker, eXp, Keller Williams, Howard Hanna, RE/MAX, BHHS Fox & Roach, Weichert, Compass, Century 21, Homesale, F.C. Tucker), each with base_url, states, render mode (static/js), selectors, robots/ToS notes, `requires_bypass` flag. Aggregators (Realtor.com/Zillow/Homes.com) listed with `requires_bypass: true` + skip reason.
- [ ] `brokerage_sources.py`: a `ConfigDrivenBrokerageSource` that instantiates from each sources.yaml entry (uses html/json extractor per render mode).
- [ ] `directory_sources.py`: permissible public directory source(s) gated by robots.
- [ ] Test: building sources from a fixture yaml registers correct count; `requires_bypass` entries are excluded from the active set.

### Task 9: Exporters

**Files:** Create `src/exporters/excel_exporter.py`, `csv_exporter.py`, `jsonl_exporter.py`.

- [ ] `excel_exporter.write(records, path)`: openpyxl, header row = exact template headers, 9 columns in order, one row per agent; nothing else.
- [ ] `csv_exporter` (9-col), `jsonl_exporter` (full rich record incl. provenance).
- [ ] Test `test_export_excel.py`: export 3 records → reopen → header matches template exactly, values in right cells, exactly 9 columns.

### Task 10: Pipeline orchestrator + CLI + logging

**Files:** Create `src/logging_setup.py`, `src/main.py`, `src/cli.py`.

- [ ] `logging_setup`: structured logging to console + `runs/<ts>.log`.
- [ ] `main.py`: subcommands `inspect-template`, `discover-sources`, `run` (crawl→normalize→validate→dedupe→store), `export`, `report`. `run` is resumable + honors `--states`, `--limit`, `--quality-first`.
- [ ] `cli.py`: argparse wiring. `python -m src.main <cmd>`.
- [ ] Smoke test: `inspect-template` prints 9 columns; `run --states IN --limit 5` against a local fixture server/mock yields rows without network.

### Task 11: Reporting

**Files:** Create report generation in `src/main.py report` → writes `docs/QUALITY_REPORT.md` + `data/exports/summary_<ts>.csv`.

- [ ] Report: counts by state, by source, duplicate rate, missing-field rate per column, confidence distribution, needs_review count, runtime, errors by source, sources skipped + why.

### Task 12: Docker + docs

**Files:** Create `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`, `docs/{PLAN,TOOLING_DECISION,SOURCES,DATA_DICTIONARY,RUNBOOK}.md`.

- [ ] Dockerfile: python:3.10-slim, install requirements, optional `playwright install --with-deps chromium`, entrypoint `python -m src.main`.
- [ ] compose: `scraper` service, mounts `./data` `./runs`, env_file `.env`.
- [ ] Docs: TOOLING_DECISION (Scrapling stealth-off rationale + per-source choice), SOURCES (table incl. skipped-bypass sources), DATA_DICTIONARY (9 cols + internal fields), RUNBOOK (build/run commands), README (compliance note + commands).

### Task 13: Live run + final outputs

- [ ] Build image, run `discover-sources --states NJ PA IN`, then `run --states NJ PA IN --quality-first` (resumable).
- [ ] Generate Excel/CSV/JSONL/SQLite + needs_review + QUALITY_REPORT. Report actual yield honestly, document coverage limits + next steps.

---

## Self-Review notes
- Spec coverage: template(T1,9), sources(T4,8), compliance(T3), normalize(T5), validate/score/dedupe(T6), storage/resume(T2), export(T9), report(T11), docker(T12), tests across T1–T9, live run(T13). ✓
- Excel-contract deviation (9 cols, rich side outputs) enforced in T1/T9. ✓
- No-bypass boundary enforced in T3 compliance gate + T8 `requires_bypass` skip. ✓
