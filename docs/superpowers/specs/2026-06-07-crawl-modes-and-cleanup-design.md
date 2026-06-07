# Crawl Modes + Data Cleanup + vanilla-base — Design

Date: 2026-06-07
Status: approved by delegation (user authorized autonomous plan + build, with
vanilla-base as the revert point)

## Goal

Extend the Crawler tab of the web UI with three crawl modes, make "Stop at
records" the default, add ways to erase exported files and recent crawls, and
clearly mark the current methodology as **vanilla-base** so future methodology
variants can be compared against (or reverted to) it.

## 1. Methodology versioning — "vanilla-base"

The current crawler (HEAD `abbfd91`: detail-link discovery + JSON-LD pipeline)
is the baseline methodology. Note: the pre-existing `vanilla` tag points to a
much older commit (`035b6fe`), so it is NOT the right revert point.

- Create git tag **`vanilla-base`** at current HEAD = the snapshot to return to.
- New work lands on branch **`feature/crawl-modes`**.
- Add a small **"methodology: vanilla-base"** badge in the UI header so the
  running methodology is visible at a glance.
- Revert path: `git checkout vanilla-base` (or reset the branch to it).

## 2. Three crawl modes

A segmented control above the form selects the mode. Shared controls (model,
Max visits, Max extracts, Stop-at-records) apply to all three.

1. **Individual** (default) — unchanged: one URL + one prompt → one run → one
   Excel. Exactly today's behavior.
2. **Bulk · shared prompt** — many URLs (one per line), a single prompt applied
   to all. Runs **serially**. Because every URL shares the prompt, they share
   columns, so the combined output is **one stacked sheet** with an added
   `Site` column identifying the seed each row came from.
3. **Queue · per-URL prompts** — an ordered list of (URL, prompt) pairs, like
   queuing jobs. Runs **serially**, one finishing before the next starts. Each
   op can have different columns, so the combined output is **one workbook with
   one sheet per operation**.

### Unifying abstraction: a "batch"

Modes 2 and 3 are both a **batch**: an ordered list of jobs `[{url, prompt}]`
plus a `combine` strategy:

- `combine = "stack"`  → mode 2 (single stacked sheet + `Site` column)
- `combine = "sheets"` → mode 3 (one sheet per op)

Each job runs as a normal crawl (reusing the existing pipeline) and is stored as
a normal run, but all runs in a batch share a `batch_id`. The combined Excel is
built from the batch's runs on demand. Individual runs remain in history and
individually exportable, so nothing is lost.

Execution is serial and reuses the existing single-job lock (the local LLM can
only do one crawl at a time anyway).

## 3. "Stop at records" as default

The `target_enabled` checkbox starts **checked**, default target **50**. The
existing JS already greys out Max visits / Max extracts when it's on, and the
backend already honors `target_records`. For batches the target applies
per-job. Users can still uncheck it to fall back to visits/extracts mode.

## 4. Erase exported files and recent crawls

Granular deletion, not just the all-or-nothing "Reset all data":

- **Recent crawls**: a delete (×) per row → `POST /delete-run/<id>` (removes the
  run and its records). Plus a **Clear history** button → removes all runs +
  records (DB only).
- **Exported files**: a delete (×) per file → `POST /delete-export/<fname>` and
  a **Clear exports** button → `POST /clear-exports`. Filenames are validated to
  stay inside `data/exports/` (no path traversal).
- "Reset all data" stays as the nuke option, and additionally clears export
  files on disk (today it only clears the DB).

## Architecture / components

### `src/crawler/store.py`
- Migration: add `batch_id` column to `ff_runs` if missing (ALTER TABLE).
- `start_run(..., batch_id=None)`.
- `runs_for_batch(batch_id) -> list[dict]`.
- `delete_run(run_id)` — delete the run and its records.
- `reset()` unchanged.

### `src/exporters/dynamic_exporter.py`
- `write_xlsx_stacked(columns, sections, path)` — stack rows from several runs
  into one sheet. `sections = [(site_label, rows), ...]`; prepend a `Site`
  column. Columns are the shared column set.
- `write_xlsx_multisheet(sheets, path)` — `sheets = [(title, columns, rows)]`;
  one sheet per entry. Sheet titles sanitized for Excel (≤31 chars, strip
  `[]:*?/\`, de-duplicated).

### `src/web/batch.py` (new, pure/testable)
- `build_jobs(mode, *, urls, prompt, pairs) -> (jobs, combine)` — turns parsed
  form input into the ordered job list + combine strategy. No Flask, no I/O.

### `src/web/app.py`
- `POST /crawl-batch` — parse mode + jobs (via `build_jobs`), validate, start a
  background batch thread. Shares `_crawl` state + lock.
- Batch runner thread — loop jobs serially, calling `runner.run_crawl` with one
  shared store + shared `batch_id`; update `_crawl["batch"]` progress
  (`{combine, total, index, jobs:[{url,state,records}], batch_id, export}`).
  After all jobs, build the combined Excel and stash its path.
- `/crawl-status` — include `batch` block.
- `GET /export-batch/<int:batch_id>` — build + download the combined Excel
  (strategy from the batch's stored combine).
- `POST /delete-run/<int:run_id>`, `POST /delete-export/<fname>`,
  `POST /clear-exports`, `POST /clear-history`.
- `/reset` also deletes export files on disk now.

### UI (`src/web/templates/index.html`)
- Segmented mode control; three form variants (show/hide), reusing shared
  controls.
- Bulk: URL textarea (one per line) + one prompt.
- Queue: repeatable (URL, prompt) rows with add/remove.
- Stop-at-records checked by default.
- Live panel shows batch progress ("Operation 2/5 — <url>") + per-op list, and
  a combined-Excel download when done.
- Recent crawls: per-row delete + Clear history; batch runs badged + linked to
  the combined Excel.
- Exported files: per-file delete + Clear exports.
- "methodology: vanilla-base" badge.
- Tasteful polish consistent with existing design tokens (impeccable skill).
- Keep the Flow tab in sync with the new pipeline (per project convention).

## Data flow

Individual: form → `/crawl` → background `run_crawl` → run + records → export →
history/exports panels (unchanged).

Batch: form → `/crawl-batch` → `build_jobs` → background loop → per-job
`run_crawl` (shared store + batch_id) → progress polled via `/crawl-status` →
on completion, combined Excel via stacked/multisheet writer → batch download
link + per-run history rows.

## Error handling

- A failing job in a batch is recorded (state=error), the batch continues to the
  next job; the combined export includes whatever succeeded.
- Export/delete endpoints validate filenames (reject traversal) and return JSON
  errors with proper status codes.
- Batch can't start if a crawl/batch is already running (409), reusing the lock.

## Testing (offline, pytest)

- exporter: stacked (Site column + correct stacking) and multisheet (per-sheet
  columns, sanitized/dedup titles) roundtrips via openpyxl.
- store: `batch_id` persisted; `runs_for_batch`; `delete_run` removes run +
  records; migration adds `batch_id` to an old-schema DB.
- `batch.build_jobs`: shared-prompt expands N urls → N jobs (same prompt,
  combine=stack); queue uses pairs (combine=sheets); blank lines/urls dropped;
  empty input rejected.
- Flask routes with the pipeline mocked: `/crawl-batch` validation + dispatch;
  `/delete-run`; `/delete-export` rejects traversal; `/clear-exports`.
- Existing 23 tests stay green.

## Out of scope (YAGNI)

- True time-based scheduling / cron (serial queue only; `/schedule` exists).
- Parallel crawling (local LLM is single-flight).
- CLI changes for batch (UI-focused; CLI single crawl unchanged).
