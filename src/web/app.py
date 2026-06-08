"""Web UI for the discovery crawler.

One page: enter a URL + a plain-language prompt, run a free-form discovery crawl
(local LLM), watch the live plan + per-call timeline, then see the discovered
records (dynamic columns from the prompt) and download the exports. Past crawls
are revisitable from the history list.

Run locally:   python -m src.web.app
In Docker:     exposed on port 5000 (see docker-compose.yml)
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, url_for

from .. import config
from ..crawler.store import FreeFormStore, now_iso
from . import batch as batch_plan
from . import runner

UI_FF_DB = config.DATA_DIR / "processed" / "ui_freeform.sqlite"
EXPORTS_DIR = config.DATA_DIR / "exports"
EXPORT_EXTS = (".xlsx", ".csv", ".jsonl")


def _ff_store() -> FreeFormStore:
    config.ensure_dirs()
    return FreeFormStore(UI_FF_DB)


# ---------------------------------------------------------------------------
# Background discovery-crawl job (one at a time; a lock guards the shared dict).
# The UI polls /crawl-status for the live plan + per-call timeline.
# ---------------------------------------------------------------------------
_crawl_lock = threading.Lock()
# Set by /crawl-stop ("Eject"); the crawl checks it cooperatively and stops,
# keeping whatever was extracted so far (records are stored incrementally).
_cancel_event = threading.Event()
_crawl: dict = {
    "state": "idle", "msg": "", "error": None, "url": "", "prompt": "",
    "events": [], "fetched": 0, "data_pages": 0, "plan": None, "llm_calls": 0,
    "trace": None, "where": None,
    # Progress/ETA: records collected so far, the optional record target, and the
    # wall-clock start (epoch seconds) so the UI can show % done + a regression ETA.
    "records": 0, "target": None, "started_at": None,
    # Batch (bulk/queue) progress; None for an individual crawl.
    "batch": None,
    # True when the user ejected the run; partial results are still shown.
    "ejected": False,
}


def _now() -> float:
    return time.time()


def _error_detail(exc: Exception) -> tuple[str, dict | None]:
    """Build a full traceback string + the culprit frame (file/line/code).

    Returns (formatted_traceback, where) where `where` describes the deepest
    stack frame — almost always the actual line that raised — so the UI can
    point at the document and line that caused the error.
    """
    full = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    where = None
    frames = traceback.extract_tb(exc.__traceback__)
    # Prefer the deepest frame inside this project; fall back to the deepest frame
    # overall (e.g. an error raised inside a library).
    root = str(config.PROJECT_ROOT)
    ours = [f for f in frames if (f.filename or "").startswith(root)]
    chosen = (ours or frames)[-1] if frames else None
    if chosen is not None:
        fname = chosen.filename or ""
        try:
            rel = str(Path(fname).resolve().relative_to(config.PROJECT_ROOT))
        except (ValueError, OSError):
            rel = fname
        where = {
            "file": rel,
            "line": chosen.lineno,
            "func": chosen.name,
            "code": (chosen.line or "").strip(),
            "type": type(exc).__name__,
        }
    return full, where


def _run_crawl_job(url: str, prompt: str, states: list[str], max_pages: int,
                   max_depth: int, model: str | None = None,
                   max_extracts: int = 10, target_records: int | None = None) -> None:
    """Execute one free-form discovery + extraction crawl on a background thread."""
    store = _ff_store()

    def on_event(ev: dict) -> None:
        kind = ev.get("kind", "")
        with _crawl_lock:
            if kind == "plan":
                _crawl["plan"] = ev
                return
            if kind == "runCompletion":
                _crawl["llm_calls"] += 1
            if "fetched" in ev:
                _crawl["fetched"] = ev["fetched"]
            if "data_pages" in ev:
                _crawl["data_pages"] = ev["data_pages"]
            if "records" in ev:
                _crawl["records"] = ev["records"]
            _crawl["events"].append({"kind": kind, "seconds": ev.get("seconds"),
                                     "url": ev.get("url", ""),
                                     "status": ev.get("status", ""),
                                     "note": ev.get("note", "")})
            _crawl["events"] = _crawl["events"][-150:]  # cap the timeline

    try:
        stats = runner.run_crawl(store, url, prompt, states, max_pages, max_depth,
                                 on_event, model, max_extracts, target_records,
                                 should_stop=_cancel_event.is_set)
        n = stats.get("records", 0)
        cols = stats.get("columns", [])
        dp = _crawl["data_pages"]
        ejected = _cancel_event.is_set() or bool(stats.get("ejected"))
        if ejected:
            msg = (f"Crawl ejected by you — {n} record(s) extracted so far "
                   f"({dp} data page(s)).")
        else:
            msg = (f"Discovery complete — {dp} data page(s), {n} record(s) extracted "
                   f"into {len(cols)} columns.")
        with _crawl_lock:
            _crawl.update(state="done", msg=msg, error=None, ejected=ejected)
    except Exception as exc:  # noqa: BLE001
        full, where = _error_detail(exc)
        with _crawl_lock:
            _crawl.update(state="error", error=str(exc), msg="",
                          trace=full, where=where)
    finally:
        store.close()


def _combine_columns(runs: list[dict]) -> list[list]:
    """Union of the runs' columns, preserving first-seen order (for stacked mode).

    Same-prompt runs almost always share columns, but the planner can vary, so a
    union avoids silently dropping a field one URL happened to surface.
    """
    out: list[list] = []
    seen: set[str] = set()
    for r in runs:
        for header, key in json.loads(r.get("columns_json") or "[]"):
            if key not in seen:
                seen.add(key)
                out.append([header, key])
    return out


def _build_batch_export(store: FreeFormStore, batch_id: str, combine: str):
    """Build the combined Excel for a batch.

    Returns ``(path, removed)`` where ``removed`` is the number of cross-site
    duplicate rows dropped (stacked/bulk mode only). ``path`` is None if empty.
    """
    from ..crawler import dedup
    from ..exporters import dynamic_exporter

    runs = store.runs_for_batch(batch_id)
    runs = [r for r in runs if store.count_for_run(r["id"]) > 0]
    if not runs:
        return None, 0
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if combine == "sheets":
        sheets = [
            (r.get("seed") or f"run{r['id']}",
             json.loads(r.get("columns_json") or "[]"),
             store.records_for_run(r["id"]))
            for r in runs
        ]
        path = EXPORTS_DIR / f"batch_queue_{ts}.xlsx"
        return dynamic_exporter.write_xlsx_multisheet(sheets, path), 0
    # default: stacked single sheet (bulk shared-prompt) + cross-site dedup
    columns = _combine_columns(runs)
    sections = [(r.get("seed") or f"run{r['id']}", store.records_for_run(r["id"])) for r in runs]
    sections, removed = dedup.dedupe_sections(sections)
    path = EXPORTS_DIR / f"batch_bulk_{ts}.xlsx"
    return dynamic_exporter.write_xlsx_stacked(columns, sections, path), removed


def _run_batch_job(jobs: list[dict], combine: str, states: list[str], max_pages: int,
                   max_depth: int, model: str | None, max_extracts: int,
                   target_records: int | None) -> None:
    """Run a batch of crawls serially on one background thread."""
    store = _ff_store()
    batch_id = f"batch-{int(_now() * 1000)}"
    total = len(jobs)

    with _crawl_lock:
        _crawl["batch"] = {
            "id": batch_id, "combine": combine, "total": total, "index": 0,
            "jobs": [{"url": j["url"], "prompt": j["prompt"], "state": "pending",
                      "records": 0, "started_at": None, "finished_at": None}
                     for j in jobs],
            "export": None,
        }

    def make_on_event(idx: int):
        def on_event(ev: dict) -> None:
            kind = ev.get("kind", "")
            with _crawl_lock:
                if kind == "plan":
                    _crawl["plan"] = ev
                    return
                if kind == "runCompletion":
                    _crawl["llm_calls"] += 1
                if "fetched" in ev:
                    _crawl["fetched"] = ev["fetched"]
                if "data_pages" in ev:
                    _crawl["data_pages"] = ev["data_pages"]
                if "records" in ev and _crawl["batch"]:
                    _crawl["batch"]["jobs"][idx]["records"] = ev["records"]
                    _crawl["records"] = ev["records"]
                _crawl["events"].append({"kind": kind, "seconds": ev.get("seconds"),
                                         "url": ev.get("url", ""),
                                         "status": ev.get("status", ""),
                                         "note": ev.get("note", "")})
                _crawl["events"] = _crawl["events"][-150:]
        return on_event

    try:
        ejected = False
        for idx, job in enumerate(jobs):
            # Eject between operations: stop launching the rest, mark them skipped.
            if _cancel_event.is_set():
                ejected = True
                with _crawl_lock:
                    for j in _crawl["batch"]["jobs"][idx:]:
                        if j["state"] == "pending":
                            j["state"] = "skipped"
                break
            with _crawl_lock:
                _crawl.update(records=0, fetched=0, data_pages=0, plan=None,
                              url=job["url"], prompt=job["prompt"])
                _crawl["batch"]["index"] = idx + 1
                _crawl["batch"]["jobs"][idx].update(state="running", started_at=_now())
            try:
                stats = runner.run_crawl(store, job["url"], job["prompt"], states,
                                         max_pages, max_depth, make_on_event(idx),
                                         model, max_extracts, target_records,
                                         batch_id=batch_id, should_stop=_cancel_event.is_set)
                with _crawl_lock:
                    _crawl["batch"]["jobs"][idx].update(
                        state="done", finished_at=_now(),
                        records=stats.get("records", 0))
            except Exception as exc:  # noqa: BLE001 - one job failing shouldn't kill the batch
                with _crawl_lock:
                    _crawl["batch"]["jobs"][idx].update(
                        state="error", finished_at=_now(), error=str(exc))
                    _crawl["events"].append({"kind": "error", "url": job["url"],
                                             "status": "error", "note": str(exc)[:160]})
            if _cancel_event.is_set():
                ejected = True  # ejected mid-operation (that op kept its partial rows)

        export_path, removed = _build_batch_export(store, batch_id, combine)
        done = sum(1 for j in _crawl["batch"]["jobs"] if j["state"] == "done")
        total_recs = sum(j.get("records", 0) for j in _crawl["batch"]["jobs"])
        dup_note = (f" {removed} cross-site duplicate(s) removed." if removed else "")
        if ejected:
            msg = (f"Batch ejected by you — {done}/{total} operation(s) done, "
                   f"{total_recs} record(s) so far.{dup_note}")
        else:
            msg = (f"Batch complete — {done}/{total} operation(s) succeeded, "
                   f"{total_recs} record(s) total.{dup_note}")
        with _crawl_lock:
            if export_path:
                _crawl["batch"]["export"] = Path(export_path).name
            _crawl["batch"]["duplicates_removed"] = removed
            _crawl.update(state="done", msg=msg, error=None, ejected=ejected)
    except Exception as exc:  # noqa: BLE001
        full, where = _error_detail(exc)
        with _crawl_lock:
            _crawl.update(state="error", error=str(exc), msg="", trace=full, where=where)
    finally:
        store.close()


def _safe_export_path(fname: str) -> Path | None:
    """Resolve a user-supplied export filename, rejecting traversal."""
    if not fname or "/" in fname or "\\" in fname or ".." in fname:
        return None
    p = (EXPORTS_DIR / fname).resolve()
    try:
        if p.parent != EXPORTS_DIR.resolve():
            return None
    except OSError:
        return None
    return p


def _export_files() -> list[str]:
    if not EXPORTS_DIR.exists():
        return []
    files = sorted(EXPORTS_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files]


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        ff = _ff_store()
        try:
            # A ?run=<id> shows that past crawl; otherwise the latest.
            sel_id = request.args.get("run", type=int)
            ff_run = ff.get_run(sel_id) if sel_id else ff.latest_run()
            ff_columns, ff_rows, ff_stats = [], [], {}
            seen_count = 0
            if ff_run:
                ff_columns = json.loads(ff_run.get("columns_json") or "[]")
                ff_rows = ff.records_for_run(ff_run["id"])
                ff_stats = json.loads(ff_run.get("stats_json") or "{}")
                # Flag rows already present in a previous EXPORT file (exports are
                # the source of truth for "already saved"). Deterministic key match.
                from ..crawler import dedup
                ledger = dedup.build_export_ledger(EXPORTS_DIR)
                if ledger:
                    for row in ff_rows:
                        files = ledger.get(dedup.record_key(row) or "\0", [])
                        if files:
                            row["__seen_in__"] = files
                            seen_count += 1
            ff_runs = []
            for r in ff.list_runs(20):
                rstats = json.loads(r.get("stats_json") or "{}")
                ff_runs.append({
                    "id": r["id"], "seed": r.get("seed"), "prompt": r.get("prompt"),
                    "started": r.get("started"),
                    "rows": ff.count_for_run(r["id"]),
                    "columns": [h for h, _ in json.loads(r.get("columns_json") or "[]")],
                    "diagnosis": rstats.get("diagnosis", ""),
                    "batch_id": r.get("batch_id"),
                    "ejected": bool(rstats.get("ejected")),
                })
            ctx = {
                "exports": _export_files(),
                "msg": request.args.get("msg", ""),
                "ff_run": ff_run,
                "ff_columns": ff_columns,
                "ff_rows": ff_rows,
                "ff_stats": ff_stats,
                "ff_runs": ff_runs,
                "ff_sel_id": ff_run["id"] if ff_run else None,
                "record_count": len(ff_rows),
                "seen_count": seen_count,
            }
            return render_template("index.html", **ctx)
        finally:
            ff.close()

    @app.route("/crawl-models")
    def crawl_models():
        """List models installed on the Ollama server + the current default."""
        from ..crawler.llm import OllamaClient

        c = OllamaClient()
        models: list[str] = []
        try:
            import httpx

            r = httpx.get(f"{c.host}/api/tags", timeout=5.0)
            r.raise_for_status()
            models = sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))
        except Exception:  # noqa: BLE001 - UI just falls back to the default
            models = []
        return jsonify(models=models, default=c.model)

    @app.route("/crawl", methods=["POST"])
    def crawl():
        url = (request.form.get("url") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        states = request.form.getlist("states")  # optional; location can live in the prompt
        model = (request.form.get("model") or "").strip() or None
        try:
            max_pages = max(1, min(80, int(request.form.get("max_pages", 25))))
        except ValueError:
            max_pages = 25
        try:
            max_extracts = max(1, min(80, int(request.form.get("max_extracts", 10))))
        except ValueError:
            max_extracts = 10
        # Optional record-target mode: a checkbox + count. When on, the crawl runs
        # until ~N records are collected (maxVisits/maxExtracts are ignored).
        target_records = None
        if request.form.get("target_enabled") in ("on", "true", "1", "yes"):
            try:
                target_records = max(1, min(5000, int(request.form.get("target_records", 50))))
            except ValueError:
                target_records = 50
        if not url or not prompt:
            return jsonify(ok=False, error="A seed URL and a prompt are both required."), 400

        with _crawl_lock:
            if _crawl["state"] == "running":
                return jsonify(ok=False, error="A discovery crawl is already running."), 409
            _cancel_event.clear()
            _crawl.update(state="running", msg="", error=None, url=url, prompt=prompt,
                          events=[], fetched=0, data_pages=0, plan=None, llm_calls=0,
                          trace=None, where=None, records=0, target=target_records,
                          started_at=_now(), batch=None, ejected=False)

        threading.Thread(
            target=_run_crawl_job,
            args=(url, prompt, states, max_pages, 3, model, max_extracts, target_records),
            daemon=True,
        ).start()
        return jsonify(ok=True, url=url, states=states, model=model)

    @app.route("/crawl-batch", methods=["POST"])
    def crawl_batch():
        """Start a serial batch crawl: bulk (shared prompt) or queue (per-URL)."""
        mode = (request.form.get("mode") or "").strip()
        model = (request.form.get("model") or "").strip() or None
        states = request.form.getlist("states")
        try:
            max_pages = max(1, min(80, int(request.form.get("max_pages", 25))))
        except ValueError:
            max_pages = 25
        try:
            max_extracts = max(1, min(80, int(request.form.get("max_extracts", 10))))
        except ValueError:
            max_extracts = 10
        target_records = None
        if request.form.get("target_enabled") in ("on", "true", "1", "yes"):
            try:
                target_records = max(1, min(5000, int(request.form.get("target_records", 50))))
            except ValueError:
                target_records = 50

        # bulk: many URLs (newline-separated) + one prompt.
        # queue: parallel url[]/prompt[] lists forming ordered (url, prompt) pairs.
        urls = None
        pairs = None
        if mode == "bulk":
            urls = (request.form.get("urls") or "").splitlines()
        elif mode == "queue":
            qurls = request.form.getlist("url[]")
            qprompts = request.form.getlist("prompt[]")
            pairs = [{"url": u, "prompt": p}
                     for u, p in zip(qurls, qprompts)]
        try:
            jobs, combine = batch_plan.build_jobs(mode, urls=urls, prompt=request.form.get("prompt"),
                                                  pairs=pairs)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400

        with _crawl_lock:
            if _crawl["state"] == "running":
                return jsonify(ok=False, error="A discovery crawl is already running."), 409
            _cancel_event.clear()
            _crawl.update(state="running", msg="", error=None,
                          url=jobs[0]["url"], prompt=jobs[0]["prompt"],
                          events=[], fetched=0, data_pages=0, plan=None, llm_calls=0,
                          trace=None, where=None, records=0, target=target_records,
                          started_at=_now(), batch=None, ejected=False)

        threading.Thread(
            target=_run_batch_job,
            args=(jobs, combine, states, max_pages, 3, model, max_extracts, target_records),
            daemon=True,
        ).start()
        return jsonify(ok=True, jobs=len(jobs), combine=combine)

    @app.route("/crawl-status")
    def crawl_status():
        with _crawl_lock:
            job = dict(_crawl)
            job["events"] = list(_crawl["events"])
        return jsonify(job)

    @app.route("/crawl-stop", methods=["POST"])
    def crawl_stop():
        """Eject: ask the running crawl/batch to stop at its next checkpoint."""
        with _crawl_lock:
            running = _crawl["state"] == "running"
        if not running:
            return jsonify(ok=False, error="No crawl is running."), 409
        _cancel_event.set()
        return jsonify(ok=True, msg="Ejecting — stopping after the current page; partial results are kept.")

    @app.route("/crawl-ack", methods=["POST"])
    def crawl_ack():
        with _crawl_lock:
            if _crawl["state"] in ("done", "error"):
                _crawl.update(state="idle", msg="", error=None, trace=None, where=None,
                              ejected=False)
        return jsonify(ok=True)

    @app.route("/run/<int:run_id>/trace")
    def run_trace(run_id: int):
        """Traceability: a past crawl's info + each step of the process (per-URL)."""
        ff = _ff_store()
        try:
            run = ff.get_run(run_id)
            if not run:
                return jsonify(ok=False, error=f"Run #{run_id} not found."), 404
            stats = json.loads(run.get("stats_json") or "{}")
            columns = json.loads(run.get("columns_json") or "[]")
            rows = ff.count_for_run(run_id)
        finally:
            ff.close()
        info = {k: stats.get(k) for k in (
            "pages_fetched", "blocked", "errors", "llm_calls", "data_pages",
            "records", "target_records", "target_met", "ejected", "engine",
            "model", "started", "finished", "diagnosis")}
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
        )

    def _delete_all_exports() -> int:
        n = 0
        if EXPORTS_DIR.exists():
            for p in EXPORTS_DIR.iterdir():
                if p.is_file() and p.suffix.lower() in EXPORT_EXTS:
                    try:
                        p.unlink()
                        n += 1
                    except OSError:
                        pass
        return n

    @app.route("/reset", methods=["POST"])
    def reset():
        """Nuke everything: crawl history + records (DB) and the export files."""
        ff = _ff_store()
        try:
            ff.reset()
        finally:
            ff.close()
        _delete_all_exports()
        with _crawl_lock:
            _crawl.update(state="idle", msg="", error=None, events=[], plan=None,
                          fetched=0, data_pages=0, llm_calls=0, trace=None, where=None,
                          records=0, target=None, started_at=None, batch=None)
        return jsonify(ok=True, msg="All crawl data and exports cleared.")

    @app.route("/clear-history", methods=["POST"])
    def clear_history():
        """Delete all crawl runs + records, but keep the exported files."""
        ff = _ff_store()
        try:
            ff.reset()
        finally:
            ff.close()
        return jsonify(ok=True, msg="Crawl history cleared.")

    @app.route("/delete-run/<int:run_id>", methods=["POST"])
    def delete_run(run_id: int):
        ff = _ff_store()
        try:
            if not ff.get_run(run_id):
                return jsonify(ok=False, error=f"Run #{run_id} not found."), 404
            ff.delete_run(run_id)
        finally:
            ff.close()
        return jsonify(ok=True, msg=f"Crawl #{run_id} deleted.")

    @app.route("/clear-exports", methods=["POST"])
    def clear_exports():
        n = _delete_all_exports()
        return jsonify(ok=True, msg=f"{n} export file(s) deleted.")

    @app.route("/delete-export/<path:fname>", methods=["POST"])
    def delete_export(fname: str):
        p = _safe_export_path(fname)
        if p is None:
            return jsonify(ok=False, error="Invalid filename."), 400
        if not p.exists():
            return jsonify(ok=False, error="File not found."), 404
        try:
            p.unlink()
        except OSError as exc:
            return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True, msg=f"{fname} deleted.")

    @app.route("/export-batch/<batch_id>")
    def export_batch(batch_id: str):
        """Build (or rebuild) the combined Excel for a batch and download it."""
        ff = _ff_store()
        try:
            runs = ff.runs_for_batch(batch_id)
            if not runs:
                return jsonify(ok=False, error="Batch not found."), 404
            # combine strategy: all-same-prompt batches stack; otherwise sheets.
            prompts = {r.get("prompt") for r in runs}
            combine = "stack" if len(prompts) == 1 else "sheets"
            path, _removed = _build_batch_export(ff, batch_id, combine)
            if not path:
                return jsonify(ok=False, error="This batch has no records to export."), 400
        finally:
            ff.close()
        return send_from_directory(EXPORTS_DIR, Path(path).name, as_attachment=True)

    @app.route("/export-run/<int:run_id>")
    def export_run(run_id: int):
        """Build (or rebuild) an Excel file for one past crawl and download it."""
        import re
        import time

        from ..exporters import dynamic_exporter

        ff = _ff_store()
        try:
            run = ff.get_run(run_id)
            if not run:
                return jsonify(ok=False, error=f"Run #{run_id} not found."), 404
            columns = json.loads(run.get("columns_json") or "[]")
            rows = ff.records_for_run(run_id)
            if not columns:
                return jsonify(ok=False, error="This crawl has no columns to export."), 400
            if not rows:
                return jsonify(ok=False, error="This crawl has no records to export."), 400
        finally:
            ff.close()

        slug = re.sub(r"[^a-z0-9]+", "-", (run.get("prompt") or "crawl").lower()).strip("-")[:40] or "crawl"
        ts = time.strftime("%Y%m%d-%H%M%S")
        fname = f"run{run_id}_{slug}_{ts}.xlsx"
        exp = config.DATA_DIR / "exports"
        exp.mkdir(parents=True, exist_ok=True)
        dynamic_exporter.write_xlsx(columns, rows, exp / fname)
        return send_from_directory(exp, fname, as_attachment=True)

    @app.route("/download/<path:fname>")
    def download(fname: str):
        return send_from_directory(config.DATA_DIR / "exports", fname, as_attachment=True)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
