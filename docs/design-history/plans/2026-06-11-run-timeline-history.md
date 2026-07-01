# Per-run Timeline History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record each crawl's full timed event stream and replay it in a "Timeline" tab when revisiting a past run.

**Architecture:** Capture every live-feed event inside `main.crawl()` into a bounded per-run buffer, persist it to a new `timeline_json` column on `ff_runs` (written by `finish_run`, including on error), serve it through the existing `/run/<id>/trace` endpoint, and render it in a Results | Timeline sub-tab in the run view by reusing the live `renderCrawlStream` function. Retire the now-redundant Trace button and per-page "visited" block.

**Tech Stack:** Python 3, Flask, SQLite (`sqlite3`), Jinja2 templates, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-06-11-run-timeline-history-design.md`

**Conventions:** Tests are offline (no Ollama/network). Run all tests with `python -m pytest -q`. Commit after each task. Branch: `feature/crawl-modes`.

---

## File Structure

- **Modify** `src/crawler/store.py` — migration for `timeline_json`; `finish_run` gains a `timeline` arg.
- **Create** `tests/test_store_timeline.py` — store round-trip + migration tests.
- **Modify** `src/web/app.py` — `/run/<id>/trace` returns a `timeline` field.
- **Modify** `tests/test_web_routes.py` — assert the new `timeline` field.
- **Modify** `src/main.py` — capture the timeline buffer; persist on success and on error (re-raise).
- **Modify** `tests/test_main_crawl.py` — error-path persists a partial timeline.
- **Modify** `src/web/templates/index.html` — Results/Timeline sub-tabs, timeline rendering, retire Trace button + visited block, update Flow note.

---

## Task 1: Persist a timeline column on `ff_runs`

**Files:**
- Modify: `src/crawler/store.py` (`_init` migration ~lines 40-45; `finish_run` ~lines 58-63)
- Test: `tests/test_store_timeline.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_timeline.py`:

```python
"""Timeline persistence on ff_runs: round-trip + additive migration."""

import json
import sqlite3

from src.crawler.store import FreeFormStore, now_iso


def _cols():
    return [("Name", "name"), ("Source URL", "source_url")]


def test_finish_run_persists_timeline(tmp_path):
    s = FreeFormStore(tmp_path / "ff.sqlite")
    rid = s.start_run("https://a.com", "p", _cols(), now_iso())
    payload = {"events": [{"kind": "visit", "seconds": 1.2, "url": "https://a.com",
                           "status": "ok", "note": "seed"}],
               "truncated": False, "total": 1}
    s.finish_run(rid, now_iso(), {"records": 0}, timeline=payload)
    run = s.get_run(rid)
    s.close()
    assert json.loads(run["timeline_json"]) == payload


def test_finish_run_without_timeline_is_null(tmp_path):
    s = FreeFormStore(tmp_path / "ff.sqlite")
    rid = s.start_run("https://a.com", "p", _cols(), now_iso())
    s.finish_run(rid, now_iso(), {"records": 0})  # no timeline arg
    run = s.get_run(rid)
    s.close()
    assert run["timeline_json"] is None


def test_migration_adds_timeline_column_to_old_db(tmp_path):
    db = tmp_path / "old.sqlite"
    # Pre-feature schema: ff_runs WITHOUT timeline_json (or batch_id).
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE ff_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, seed TEXT, "
        "prompt TEXT, columns_json TEXT, started TEXT, finished TEXT, stats_json TEXT);"
        "INSERT INTO ff_runs (seed, prompt) VALUES ('https://a.com', 'p');"
    )
    conn.commit()
    conn.close()
    s = FreeFormStore(db)  # opening runs the migration
    run = s.get_run(1)
    s.close()
    assert "timeline_json" in run and run["timeline_json"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_store_timeline.py -q`
Expected: FAIL — `finish_run()` got an unexpected keyword argument `timeline`, and `timeline_json` missing from the row.

- [ ] **Step 3: Add the migration**

In `src/crawler/store.py`, in `_init`, the block that adds `batch_id` (currently):

```python
        cols = {c[1] for c in self.conn.execute("PRAGMA table_info(ff_runs)").fetchall()}
        if "batch_id" not in cols:
            self.conn.execute("ALTER TABLE ff_runs ADD COLUMN batch_id TEXT")
        self.conn.commit()
```

becomes:

```python
        cols = {c[1] for c in self.conn.execute("PRAGMA table_info(ff_runs)").fetchall()}
        if "batch_id" not in cols:
            self.conn.execute("ALTER TABLE ff_runs ADD COLUMN batch_id TEXT")
        # Per-run timeline (full timed event stream) for history replay.
        if "timeline_json" not in cols:
            self.conn.execute("ALTER TABLE ff_runs ADD COLUMN timeline_json TEXT")
        self.conn.commit()
```

- [ ] **Step 4: Add the `timeline` argument to `finish_run`**

Replace `finish_run` (currently):

```python
    def finish_run(self, run_id: int, finished: str, stats: dict) -> None:
        self.conn.execute(
            "UPDATE ff_runs SET finished=?, stats_json=? WHERE id=?",
            (finished, json.dumps(stats), run_id),
        )
        self.conn.commit()
```

with:

```python
    def finish_run(self, run_id: int, finished: str, stats: dict,
                   timeline: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE ff_runs SET finished=?, stats_json=?, timeline_json=? WHERE id=?",
            (finished, json.dumps(stats),
             json.dumps(timeline) if timeline is not None else None, run_id),
        )
        self.conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store_timeline.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/crawler/store.py tests/test_store_timeline.py
git commit -m "feat(store): persist per-run timeline_json (additive migration)"
```

---

## Task 2: Serve the timeline from `/run/<id>/trace`

**Files:**
- Modify: `src/web/app.py` (`run_trace`, ~lines 537-564)
- Test: `tests/test_web_routes.py` (add two tests near `test_run_trace_returns_info_and_steps`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_routes.py` (the file already imports `FreeFormStore, now_iso`):

```python
def test_run_trace_includes_timeline(client, tmp_path):
    s = FreeFormStore(tmp_path / "ui.sqlite")
    rid = s.start_run("https://a.com", "p", [("Name", "name")], now_iso())
    tl = {"events": [{"kind": "visit", "seconds": 0.5, "url": "https://a.com",
                      "status": "ok", "note": "seed"}],
          "truncated": False, "total": 1}
    s.finish_run(rid, now_iso(), {"records": 0}, timeline=tl)
    s.close()
    j = client.get(f"/run/{rid}/trace").get_json()
    assert j["ok"] and j["timeline"]["total"] == 1
    assert j["timeline"]["events"][0]["kind"] == "visit"


def test_run_trace_timeline_null_when_absent(client, tmp_path):
    s = FreeFormStore(tmp_path / "ui.sqlite")
    rid = s.start_run("https://a.com", "p", [("Name", "name")], now_iso())
    s.finish_run(rid, now_iso(), {"records": 0})  # no timeline
    s.close()
    j = client.get(f"/run/{rid}/trace").get_json()
    assert j["ok"] and j["timeline"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_routes.py -k timeline -q`
Expected: FAIL — `KeyError: 'timeline'` (the endpoint doesn't return that field yet).

- [ ] **Step 3: Add `timeline` to the trace response**

In `src/web/app.py`, inside `run_trace`, after `rows = ff.count_for_run(run_id)` and before `finally: ff.close()`, add the timeline parse (it reads from the `run` row dict already fetched):

```python
            rows = ff.count_for_run(run_id)
            timeline = json.loads(run.get("timeline_json") or "null")
```

Then add `timeline=timeline,` to the `return jsonify(...)` call, e.g.:

```python
        return jsonify(
            ok=True,
            run={"id": run["id"], "seed": run.get("seed"), "prompt": run.get("prompt"),
                 "started": run.get("started"), "finished": run.get("finished"),
                 "batch_id": run.get("batch_id")},
            columns=[h for h, _ in columns],
            rows=rows,
            info=info,
            field_coverage=stats.get("field_coverage") or {},
            steps=stats.get("visited") or [],
            timeline=timeline,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_routes.py -k timeline -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/web/app.py tests/test_web_routes.py
git commit -m "feat(web): /run/<id>/trace returns the persisted timeline"
```

---

## Task 3: Capture the timeline in `main.crawl()` and persist on success

**Files:**
- Modify: `src/main.py` (`crawl()` — event wiring ~lines 68-71, 158-161, 197; success `finish_run` ~line 303)

This task wires up capture and the success path. The error path is Task 4.

- [ ] **Step 1: Add the timeline buffer + event recorder**

In `src/main.py` `crawl()`, replace this block (currently ~lines 68-71):

```python
    client = OllamaClient(model=model)
    if on_event:
        # Every LLM completion emits a timed "runCompletion" to the live feed.
        client.on_call = on_event
```

with:

```python
    client = OllamaClient(model=model)
    # Per-run timeline: capture every live-feed event so a finished run can be
    # replayed from history. The live UI keeps only the last ~150 events for
    # render speed; the persisted timeline keeps up to TIMELINE_CAP.
    TIMELINE_CAP = 2000
    _timeline: list[dict] = []

    def _record(ev: dict) -> None:
        # The "plan" event feeds the live UI's own box, not the scrolling stream;
        # the persisted timeline mirrors the stream, so plan is not buffered here.
        if ev.get("kind") != "plan":
            _timeline.append({"kind": ev.get("kind", ""), "seconds": ev.get("seconds"),
                              "url": ev.get("url", ""), "status": ev.get("status", ""),
                              "note": ev.get("note", "")})
        if on_event:
            on_event(ev)

    def _timeline_payload() -> dict:
        total = len(_timeline)
        return {"events": _timeline[-TIMELINE_CAP:], "truncated": total > TIMELINE_CAP,
                "total": total}

    # Every LLM completion emits a timed "runCompletion"; route it through _record.
    client.on_call = _record
```

> Note: leave the `plan` event block (the `if on_event: on_event({"kind": "plan", ...})` around line 90) UNCHANGED — `_record` deliberately skips `plan`, and the live UI still receives it directly.

- [ ] **Step 2: Route the extract event + crawler events through `_record`**

In `_extract_page`, replace (currently ~lines 158-161):

```python
        if on_event:
            on_event({"kind": "extract", "url": page_url, "status": "extracted",
                      "fetched": 0, "data_pages": dp_count, "records": kept,
                      "note": f"+{added} record(s) ({kept} total)"})
```

with:

```python
        _record({"kind": "extract", "url": page_url, "status": "extracted",
                 "fetched": 0, "data_pages": dp_count, "records": kept,
                 "note": f"+{added} record(s) ({kept} total)"})
```

In the `DiscoveryCrawler(...)` construction (currently ~line 197), change `on_event=on_event` to `on_event=_record`:

```python
        same_domain=same_domain, on_event=_record, on_data_page=on_data_page,
```

- [ ] **Step 3: Persist the timeline on the success path**

Replace the success `finish_run` call (currently ~line 303):

```python
    ff.finish_run(run_id, now_iso(), ex_stats)
```

with:

```python
    ff.finish_run(run_id, now_iso(), ex_stats, timeline=_timeline_payload())
```

- [ ] **Step 4: Verify existing main-crawl tests still pass**

Run: `python -m pytest tests/test_main_crawl.py -q`
Expected: PASS (2 passed) — the existing tests assert records/dedup/refetch; routing events through `_record` must not change them.

- [ ] **Step 5: Verify the timeline is captured on a successful crawl**

Add this test to `tests/test_main_crawl.py` (uses the existing `site` fixture):

```python
def test_successful_crawl_persists_timeline(site):
    fetcher, store = site
    out = _crawl(store)
    run = store.get_run(out["run_id"])
    import json
    tl = json.loads(run["timeline_json"])
    assert tl is not None and tl["truncated"] is False
    # Extraction emits at least one timed "extract" event into the stream.
    assert any(e["kind"] == "extract" for e in tl["events"])
```

Run: `python -m pytest tests/test_main_crawl.py::test_successful_crawl_persists_timeline -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/test_main_crawl.py
git commit -m "feat(crawl): capture full timed event timeline; persist on success"
```

---

## Task 4: Persist a partial timeline when a crawl errors mid-run

**Files:**
- Modify: `src/main.py` (`crawl()` — wrap the post-`start_run` body in try/except/finally)
- Test: `tests/test_main_crawl.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_main_crawl.py`:

```python
def test_errored_crawl_persists_partial_timeline(site, monkeypatch):
    fetcher, store = site
    import json
    import src.crawler.discovery as disco

    class BoomCrawler:
        def __init__(self, *a, on_event=None, **k):
            self._oe = on_event

        def run(self, url):
            if self._oe:
                self._oe({"kind": "visit", "url": url, "status": "ok", "note": "probe"})
            raise RuntimeError("kaboom")

    monkeypatch.setattr(disco, "DiscoveryCrawler", BoomCrawler)

    with pytest.raises(RuntimeError, match="kaboom"):
        _crawl(store)

    run = store.latest_run()
    assert run["finished"]                       # finished despite the error
    tl = json.loads(run["timeline_json"])
    assert tl and tl["events"][0]["status"] == "ok"   # partial timeline captured
    stats = json.loads(run["stats_json"])
    assert "error" in (stats.get("diagnosis") or "").lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_main_crawl.py::test_errored_crawl_persists_partial_timeline -q`
Expected: FAIL — the exception propagates (good) but the run is never finished, so `run["finished"]` is None and `timeline_json` is None.

- [ ] **Step 3: Wrap the post-`start_run` body in try/except/finally**

In `src/main.py`, the body from just after `run_id` is created through the end of `crawl()` must be wrapped so an error still finishes the run with a partial timeline.

(3a) Find the `run_id` line (currently ~line 130):

```python
    run_id = ff.start_run(url, prompt, columns, now_iso(), batch_id=batch_id) if ff else None
```

Immediately AFTER it, insert `    try:` and indent the entire remaining body of the function (from the next line, `extractor = ...`, down to and including the final `return out`) by 4 additional spaces. **Do not change any of those lines other than their indentation.**

(3b) The function currently ends like this (the success `finish_run` was already updated in Task 3):

```python
    if export_after and kept:
        rows = ff.records_for_run(run_id)
        ...
        out["export"] = {"paths": paths, "exported": kept}

    if own_store:
        ff.close()
    return out
```

After indenting under `try:`, **remove** the old `if own_store: ff.close()` lines (the close moves to a `finally`), keep `return out` inside the `try`, and append the `except`/`finally`. The tail of the function must read:

```python
        if export_after and kept:
            rows = ff.records_for_run(run_id)
            exp = config.DATA_DIR / "exports"
            slug = re.sub(r"[^a-z0-9]+", "_", (intent.entity or "records").lower()).strip("_")
            paths = {
                "xlsx": dynamic_exporter.write_xlsx(columns, rows, exp / f"{slug}_{ts}.xlsx"),
                "csv": dynamic_exporter.write_csv(columns, rows, exp / f"{slug}_{ts}.csv"),
                "jsonl": dynamic_exporter.write_jsonl(columns, rows, exp / f"{slug}_{ts}.jsonl"),
            }
            for k, v in paths.items():
                print(f"  {k}: {v}")
            out["export"] = {"paths": paths, "exported": kept}

        return out
    except Exception as exc:  # noqa: BLE001 - persist a partial timeline, then re-raise
        if ff is not None and run_id is not None:
            try:
                ff.finish_run(run_id, now_iso(), {
                    "records": ff.count_for_run(run_id),
                    "error": str(exc),
                    "diagnosis": f"Crawl errored: {exc}",
                    "finished": now_iso(),
                }, timeline=_timeline_payload())
            except Exception:  # noqa: BLE001 - never mask the original failure
                log.warning("could not persist partial timeline for errored run %s", run_id)
        raise
    finally:
        if own_store and ff is not None:
            ff.close()
```

> Boundary check after editing: `run_id = ...` is at the base indent (4 spaces); everything from `extractor = ...` through `return out` is indented one level deeper (8 spaces); `except`/`finally` align with `try`. The early `return out` for the non-extract path (inside the `if not extract:` branch) is now also inside the `try` — that is fine; `finally` runs as a harmless no-op (ff is None there).

- [ ] **Step 4: Run the new test + the full main-crawl suite**

Run: `python -m pytest tests/test_main_crawl.py -q`
Expected: PASS (all tests, including the new error-path test and the success/timeline tests from Task 3).

- [ ] **Step 5: Commit**

```bash
git add src/main.py tests/test_main_crawl.py
git commit -m "feat(crawl): persist partial timeline on errored runs, then re-raise"
```

---

## Task 5: Add Results | Timeline sub-tabs to the run view

**Files:**
- Modify: `src/web/templates/index.html` (run-view section ~lines 749-808; `renderCrawlStream` ~lines 1084-1102; JS init area)

No automated test (vanilla-JS template); verified manually in Task 8.

- [ ] **Step 1: Generalize `renderCrawlStream` to accept a target element**

Replace the function signature line and its first body line (currently ~lines 1084-1086):

```javascript
  function renderCrawlStream(events) {
    if (!crawlStream) return;
    crawlStream.innerHTML = '';
```

with:

```javascript
  function renderCrawlStream(events, target) {
    target = target || crawlStream;
    if (!target) return;
    target.innerHTML = '';
```

Then, within that function, change the final `crawlStream.appendChild(row);` to `target.appendChild(row);`. (The live caller `renderCrawlStream(s.events || [])` keeps working via the default.)

- [ ] **Step 2: Insert the sub-tab switch + wrap the Results content**

In `index.html`, immediately AFTER the request-summary `.plan` block (the `<div class="plan" ...>...</div>` that ends ~line 754) and BEFORE the `{% if seen_count %}` dupe banner, insert:

```html
    <div class="segment" id="run-view-seg" role="tablist" aria-label="Run view"
         style="margin-bottom:var(--sp-3)">
      <button type="button" data-view="results" class="active" role="tab" aria-selected="true">Results</button>
      <button type="button" data-view="timeline" role="tab" aria-selected="false">Timeline</button>
    </div>
    <div class="run-view" data-view="results">
```

Then, immediately AFTER the Results content ends — the line `    {% endif %}` that closes the `{% if ff_rows %}…{% else %}…{% endif %}` block (currently ~line 807, just before `  </section>`) — insert the closing tag for the results wrapper and the whole Timeline view:

```html
    </div><!-- /run-view results -->
    <div class="run-view" data-view="timeline" hidden data-run="{{ ff_sel_id }}">
      <div id="run-timeline-head" class="trace-stats"></div>
      <p id="run-timeline-cap" class="meta" style="white-space:normal" hidden></p>
      <div id="run-timeline-stream" class="stream" style="max-height:420px; overflow:auto"></div>
      <div id="run-timeline-empty" class="trace-empty" hidden>No timeline was recorded for this crawl.</div>
    </div>
```

- [ ] **Step 3: Add the sub-tab switching + lazy timeline loader (JS)**

Add this self-running block in the `<script>` section (e.g. right after the `initTabs` IIFE that handles the Crawler/Flow tabs):

```javascript
  /* ---- Run view sub-tabs (Results / Timeline) -------------------- */
  (function initRunView() {
    var seg = $('#run-view-seg');
    if (!seg) return;                       // no run selected
    var views = $$('.run-view');
    var timelineLoaded = false;

    function show(view) {
      seg.querySelectorAll('button').forEach(function (b) {
        var on = b.getAttribute('data-view') === view;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      views.forEach(function (v) { v.hidden = v.getAttribute('data-view') !== view; });
      if (view === 'timeline' && !timelineLoaded) { timelineLoaded = true; loadTimeline(); }
    }

    function kvChips(info) {
      function kv(label, val) {
        return (val === null || val === undefined || val === '') ? '' :
          '<span class="kv">' + esc(label) + ' <b>' + esc(val) + '</b></span>';
      }
      var i = info || {};
      return [kv('records', i.records), kv('pages fetched', i.pages_fetched),
              kv('data pages', i.data_pages), kv('blocked', i.blocked),
              kv('errors', i.errors), kv('LLM calls', i.llm_calls),
              kv('model', i.model),
              (i.ejected ? '<span class="kv"><b style="color:var(--warn)">ejected</b></span>' : '')
             ].join('');
    }

    function loadTimeline() {
      var panel = $('.run-view[data-view="timeline"]');
      var id = panel && panel.getAttribute('data-run');
      var head = $('#run-timeline-head'), cap = $('#run-timeline-cap'),
          stream = $('#run-timeline-stream'), empty = $('#run-timeline-empty');
      if (!id) { if (empty) empty.hidden = false; return; }
      fetch('/run/' + id + '/trace').then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) { if (empty) { empty.textContent = d.error || 'Could not load timeline'; empty.hidden = false; } return; }
        if (head) head.innerHTML = kvChips(d.info);
        var tl = d.timeline;
        if (!tl || !tl.events || !tl.events.length) { if (empty) empty.hidden = false; return; }
        renderCrawlStream(tl.events, stream);
        if (tl.truncated && cap) {
          cap.textContent = 'Showing the most recent ' + tl.events.length.toLocaleString() +
            ' events of ' + (tl.total || 0).toLocaleString() + ' — older events were not recorded.';
          cap.hidden = false;
        }
      }).catch(function () { if (empty) { empty.textContent = 'Network error loading timeline'; empty.hidden = false; } });
    }

    seg.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () { show(b.getAttribute('data-view')); });
    });
  })();
```

> `$`, `$$`, `esc`, and `renderCrawlStream` are already defined in this file; `kvChips` mirrors the old `renderTrace` chip helper (removed in Task 6).

- [ ] **Step 4: Manual smoke (deferred to Task 8)** — covered by the end-to-end UI verification.

- [ ] **Step 5: Commit**

```bash
git add src/web/templates/index.html
git commit -m "feat(ui): Results | Timeline sub-tabs; replay persisted timeline on revisit"
```

---

## Task 6: Retire the Trace button, the visited block, and dead trace JS

**Files:**
- Modify: `src/web/templates/index.html` (history-list row ~lines 848-855; visited block ~lines 794-805; trace JS ~lines 1468-1509; click handler ~lines 1464-1465)

- [ ] **Step 1: Remove the per-row "Trace" button + inline trace panel**

In the "Recent crawls" list item, delete the Trace `<button>` (currently ~lines 848-851) and the inline trace panel `<div class="trace-panel" data-trace="{{ r.id }}" hidden></div>` (currently ~line 855). Leave the Reuse/Excel/Combined and delete controls intact.

- [ ] **Step 2: Remove the per-page "visited" block from the empty state**

In the `{% else %}` no-records branch, delete the `{% if ff_stats.visited %} … {% endif %}` block (currently ~lines 794-805). Keep the `<b>No records extracted for this crawl.</b>` line and the `diagnosis` paragraph above it. The branch becomes:

```html
    {% else %}
      <div class="empty" style="padding:var(--sp-5)">
        <b>No records extracted for this crawl.</b>
        {% if ff_stats.diagnosis %}<p style="color:var(--warn); max-width:70ch">⚠ {{ ff_stats.diagnosis }}</p>{% endif %}
        <p class="meta" style="white-space:normal">Open the <b>Timeline</b> tab above to see what happened, page by page.</p>
      </div>
    {% endif %}
```

- [ ] **Step 3: Remove the dead trace JS**

Delete the now-unused JS: the `.run-trace` branch in the history click handler (currently ~lines 1464-1465):

```javascript
    var trc = e.target.closest('.run-trace');
    if (trc) { toggleTrace(trc); return; }
```

and the entire `stepStatusClass`, `renderTrace`, and `toggleTrace` block (currently ~lines 1468-1509). Do NOT remove the `clearHistBtn` handler that follows them.

- [ ] **Step 4: Verify no dangling references**

Run: `python - <<'PY'
import pathlib, re
t = pathlib.Path("src/web/templates/index.html").read_text(encoding="utf-8")
for name in ("toggleTrace", "renderTrace", "stepStatusClass", "run-trace", "trace-panel"):
    assert name not in t, f"leftover reference: {name}"
print("clean")
PY`
Expected: prints `clean`.

- [ ] **Step 5: Commit**

```bash
git add src/web/templates/index.html
git commit -m "refactor(ui): retire Trace button + visited block (superseded by Timeline tab)"
```

---

## Task 7: Sync the Flow diagram note

**Files:**
- Modify: `src/web/templates/index.html` (the `FLOW` object's Output stage, ~lines 1612-1618)

Per the standing flow-diagram-sync convention, the Flow tab must describe the new behavior.

- [ ] **Step 1: Add a History note to the Output stage**

In the `FLOW` object's Output stage `notes` array, after the existing `{ lbl: 'Live', ... }` note, add:

```javascript
                { lbl: 'History', text: 'Every run records its full timed event timeline; revisit a past crawl and open the Timeline tab to replay it (capped at 2,000 events, oldest dropped).' },
```

- [ ] **Step 2: Commit**

```bash
git add src/web/templates/index.html
git commit -m "docs(ui): note persisted replayable timeline in the Flow diagram"
```

---

## Task 8: Full verification + manual UI check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests pass (including `test_store_timeline.py`, the new `test_web_routes.py` timeline tests, and the new `test_main_crawl.py` success + error timeline tests).

- [ ] **Step 2: Rebuild the UI container**

Run: `docker compose up -d --build ui`
Expected: container rebuilds and starts cleanly.

- [ ] **Step 3: Manual smoke test**

In the browser:
1. Run a small crawl (or open an existing run from "Recent crawls" — note: only runs created AFTER this change have a timeline).
2. In the run view, confirm the **Results** / **Timeline** sub-tabs appear.
3. Click **Timeline** — confirm the stat header + the timed event stream render, looking like the live feed.
4. Confirm the per-row **Trace** button is gone from "Recent crawls".
5. Open the **Flow** tab — confirm the new History note appears under Output.

- [ ] **Step 4: Final commit (if any manual fixes were needed)**

```bash
git add -A
git commit -m "test: verify per-run timeline history end-to-end"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** Capture (§1 → Task 3), error path (§1a → Task 4), persist (§2 → Task 1), serve (§3 → Task 2), display + cap caption + empty state (§4 → Task 5), retire Trace + visited block (§5 → Task 6), Flow sync → Task 7. All spec sections mapped.
- **Type consistency:** `_timeline_payload()` returns `{events, truncated, total}` everywhere; `finish_run(..., timeline=...)` signature matches all callers (Tasks 1, 3, 4); `/run/<id>/trace` returns `timeline` consumed by `loadTimeline` (Task 5).
- **Placeholders:** none — every code step shows real code; the one large re-indent (Task 4 Step 3) is bounded by quoted anchor lines with an explicit "indentation only" instruction.
