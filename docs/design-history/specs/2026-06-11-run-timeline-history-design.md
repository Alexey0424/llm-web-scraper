# Per-run timeline history — design

**Date:** 2026-06-11
**Branch:** `feature/crawl-modes`
**Status:** Approved in principle; awaiting spec review.

## Problem

While a crawl runs, the UI shows a rich, timed event stream — every LLM call
(`runCompletion` with seconds), every page visit, every extract — each with a
timestamp, URL, status, and note. That stream lives only in memory
(`_crawl["events"]`, capped at the last 150 entries) and is **discarded when the
crawl finishes**.

When the user revisits a past crawl, they only get a flattened per-page summary
(`stats["visited"]`): status + note per page, with **no timing and no LLM-call
granularity**. The live timeline they watched is gone.

The user wants the **full timed event stream recorded per run and replayed**
when they click into a recent crawl, presented as a tab alongside the results.

## Goal

When revisiting a run, the user sees a **Timeline** tab containing a faithful
replay of the live event feed — identical in look to what they watched run —
next to the existing **Results** (records table) tab.

## Non-goals

- Backfilling timelines for crawls already in the database.
- Any change to the live-crawl experience itself.
- Saving a timeline for failures that happen **before the run row exists**
  (i.e. during intent planning, before `start_run`) — there is no run to attach
  it to. Every failure after the run is created is captured (see Error path).

## Approach (chosen: A)

Capture the complete event stream inside the crawl pipeline and store it in a
dedicated column on `ff_runs`, loaded lazily only when the Timeline tab opens.

Rejected alternatives:
- **B — store in `stats_json`.** `stats_json` is read for all ~20 history rows
  on every page load (diagnosis/ejected badges); bloating it with full
  timelines slows the "Recent crawls" list.
- **C — normalized `ff_events` table.** The data is replay-only; we never query
  it by field. A relational table is overkill.

## Design

### 1. Capture — `src/main.py`

`crawl()` already receives an `on_event` callback. Wrap it so every emitted
event is also appended to an in-run `timeline` buffer. Each entry uses the exact
shape the UI already renders:

```python
{"kind": ..., "seconds": ..., "url": ..., "status": ..., "note": ...}
```

- The buffer is **bounded to 2000 events**. The live UI keeps only the last 150
  for render performance; persistence keeps far more but still caps to avoid
  pathological bloat.
- If the run produces more than 2000 events, keep the **most recent 2000** (so
  the end of the run — final extracts — is always present) and record that the
  timeline was truncated.
- Stored as an object so the truncation state survives:
  `{"events": [...], "truncated": bool, "total": <int>}`.
- The buffer is scoped per `crawl()` call, which is exactly one run — so each
  job in a bulk/queue batch naturally gets its own timeline.
- The wrapper appends every event the live feed shows, **skipping `kind=="plan"`**
  — the live UI intercepts the plan event into its own box rather than the
  stream, and the Timeline tab mirrors that. The buffer starts collecting from
  the top of `crawl()`, so planning-phase LLM calls are included once the run
  exists.

### 1a. Error path — `src/main.py`

Today `crawl()` lets exceptions propagate to `app.py`, leaving the DB run row
unfinished (no timeline). To capture a partial timeline on failure:

- Wrap the body **after `start_run`** (the crawl + extraction) in `try/except`.
- On exception, if a run row exists, call `finish_run` with the partial
  timeline and a minimal error-marked stats blob (`records` collected so far,
  `error`, and a `diagnosis` like *"Crawl errored: <message>"*), then
  **re-raise** so `app.py` still surfaces the error to the UI exactly as it does
  now (state=error, traceback, culprit frame).
- The persist-on-error call is itself wrapped so a secondary failure while
  saving never masks the original error.
- Net effect: an errored run now appears in history finished, with whatever
  records it managed to extract and a full partial timeline.

### 2. Persist — `src/crawler/store.py`

- Additive migration: add a `timeline_json TEXT` column to `ff_runs`, using the
  same "ALTER if missing" pattern already used for `batch_id`.
- `finish_run(run_id, finished, stats, timeline=None)` gains an optional
  `timeline` argument written into `timeline_json`.
- Pre-existing runs (and failures before the run row is created) leave the
  column `NULL`. Errored-mid-crawl runs now reach `finish_run` via the error
  path (§1a), so they keep their partial timeline.

### 3. Serve — `src/web/app.py`

The existing `/run/<int:run_id>/trace` endpoint gains a `timeline` field built
from `timeline_json` (parsed object, or `null` if absent). It continues to
return the compact `info` block (records, pages fetched, LLM calls, model,
diagnosis, ejected) so the Timeline tab can show a small stat header above the
stream. No new endpoint is added.

### 4. Display — `src/web/templates/index.html`

- Add a sub-tab switch inside the results panel, styled with the existing
  `.segment` pattern (the same control used for Individual/Bulk/Queue):
  **Results** | **Timeline**.
- **Results tab:** the current `#ff-table`, dupe banner, and stats — unchanged.
- **Timeline tab:** a `.stream` container rendered by **reusing the existing
  `renderCrawlStream(events)` function**, so a revisited run looks identical to
  the live feed. Above the stream, a compact stat header (reusing the
  `renderTrace` stat chips) gives context.
- The timeline loads **lazily** the first time the Timeline tab is opened for a
  run, via `/run/<id>/trace`.
- **Cap surfaced in the UI:** when `timeline.truncated` is true, the Timeline
  tab shows a caption — e.g. *"Showing the most recent 2,000 events of N — older
  events were not recorded."*
- Empty state: a run with no recorded timeline (old or errored) shows *"No
  timeline was recorded for this crawl."*

### 5. Retire the Trace button + visited block

- Remove the per-row **"Trace"** button and its inline `.trace-panel` from the
  "Recent crawls" list (`index.html`).
- Remove the now-redundant `ff_stats.visited` steps block from the revisit view
  — the Timeline tab supersedes it.
- Remove the now-dead inline-trace JS (`toggleTrace`, `renderTrace`'s steps
  rendering) — but keep the stat-chip helper, reused by the Timeline header.
- The `/run/<id>/trace` endpoint is kept (now serving the Timeline tab).

## Data flow

```
crawl() emits events ──► wrapped on_event ──► in-run timeline buffer (≤2000)
                                                      │
                                          finish_run(..., timeline)
                                                      │
                                             ff_runs.timeline_json
                                                      │
                            GET /run/<id>/trace ──► { info, timeline }
                                                      │
                          Timeline tab ──► renderCrawlStream(timeline.events)
```

## Edge cases

- **Errored run:** the error path (§1a) finishes the run with its partial
  timeline + an error diagnosis → Timeline tab replays what happened up to the
  failure.
- **Failure before the run exists** (planning error): no run row → nothing to
  attach; behaves as today.
- **Pre-existing run:** column NULL → empty state.
- **Batch job:** one `crawl()` per job → one timeline per run row; revisiting
  any job in the batch shows that job's own timeline.
- **Truncated run (>2000 events):** last 2000 kept; UI caption shown.
- **Zero-record run:** still has a timeline (visits/blocks/LLM calls) → useful
  for diagnosing why nothing was found.

## Testing

- `store.py`: round-trip a timeline through `start_run` → `finish_run(timeline=…)`
  → `get_run`, asserting `timeline_json` persists and old runs read back NULL.
- `app.py` (`tests/test_web_routes.py`): `/run/<id>/trace` returns the `timeline`
  object for a run that has one, and `timeline: null` for one that doesn't.
- Migration: opening an old DB (no `timeline_json` column) adds the column
  without error and existing rows read back NULL.
- Error path: a `crawl()` that raises after `start_run` still finishes the run
  with a non-NULL timeline and an error diagnosis, and the exception still
  propagates to the caller.

## Flow-diagram sync

The UI "Flow" tab (`FLOW` object in `index.html`) describes the live timeline
under the Output stage. Update that note to mention the timeline is now
**persisted per run and replayable** from history. (Per the standing
flow-diagram-sync convention.)
