# Future improvement ideas (for review)

Date: 2026-06-07. These are candidates, not commitments — picked for high value
vs. effort. Nothing here is built yet; "Done this round" lists what already shipped.

## Done this round
- Three crawl modes: Individual, Bulk (shared prompt → stacked Excel), Queue
  (per-URL prompts → one sheet per op).
- "Stop at records" is the default.
- Live progress: % complete, elapsed, regression-based ETA (record-target mode),
  per-operation step dots for batches.
- Delete a single crawl, clear history, delete a single export, clear exports,
  reset everything (now also wipes export files).
- `vanilla-base` tag + UI methodology badge.
- **Cross-batch dedup** (item 8): bulk/stacked combine drops cross-site duplicates
  by deterministic canonical key (email → phone → name+location); reports count.
- **Already-exported recognition** (`src/crawler/dedup.py`): each discovered record
  is flagged if it already appears in a previous export `.xlsx` (the export files
  are the source of truth; the ledger is cached + invalidated by file mtime, so
  deleting a file drops its rows). UI shows a banner + an "Exported" status column.

## Remaining dedup enhancement
- **Fuzzy / probabilistic matching** for records that have *no* email/phone (name
  only) — Jaro-Winkler / token similarity, or a library (recordlinkage / Splink).
  Deliberately deferred: the deterministic key is correct and fast for data that
  carries identifiers; fuzzy adds recall at the cost of false-merge risk + deps.

## High value, low effort
1. **Pause / cancel a running crawl or batch.** A stop button that sets a flag the
   crawl loop checks between pages. Today a run can only finish or error.
2. **Persisted batches view.** A dedicated "Batches" list (grouped by `batch_id`)
   instead of badged rows mixed into history, each with its combined download.
3. **CSV/JSONL alongside the combined Excel.** The single-run path already writes
   all three; batches currently only build the combined `.xlsx`.
4. **Re-run a batch.** "Reuse" exists for single runs; add it for a whole batch
   (reload all its URLs+prompts back into Queue mode).
5. **Throughput readout.** Records/min and pages/min next to the ETA — cheap to
   add from the samples we already collect.

## Medium effort
6. **True scheduling.** Run a batch at a time or on a cron (the `/schedule` skill
   exists; wiring a saved batch to it would make "program the operation" literal).
7. **Per-job overrides in Queue mode.** Let each operation set its own Max
   visits / target, not just a batch-wide value.
8. **Dedup across a batch.** Optional cross-site dedup (by email/name) when
   stacking bulk results, with a "duplicates removed: N" note.
9. **Column mapping for bulk stacking.** When the planner derives slightly
   different keys per URL, let the user map them to one canonical column.
10. **Resumable batches.** Persist queue progress so a restart continues where it
    left off (the store already records each run).

## Larger / strategic
11. **A second methodology to compare against `vanilla-base`** — e.g. the
    URL-pattern dedup approach from `_url_paper.txt`. Switchable per crawl, with
    a side-by-side quality comparison. This is the reason we tagged vanilla-base.
12. **Cost/quality telemetry per run** (LLM calls, tokens, time) charted over
    history to tune model choice.
13. **Export to Google Sheets / a CRM** instead of only files.
