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
import traceback
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

from .. import config
from ..crawler.store import FreeFormStore
from . import runner

UI_FF_DB = config.DATA_DIR / "processed" / "ui_freeform.sqlite"


def _ff_store() -> FreeFormStore:
    config.ensure_dirs()
    return FreeFormStore(UI_FF_DB)


# ---------------------------------------------------------------------------
# Background discovery-crawl job (one at a time; a lock guards the shared dict).
# The UI polls /crawl-status for the live plan + per-call timeline.
# ---------------------------------------------------------------------------
_crawl_lock = threading.Lock()
_crawl: dict = {
    "state": "idle", "msg": "", "error": None, "url": "", "prompt": "",
    "events": [], "fetched": 0, "data_pages": 0, "plan": None, "llm_calls": 0,
    "trace": None, "where": None,
}


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
            _crawl["events"].append({"kind": kind, "seconds": ev.get("seconds"),
                                     "url": ev.get("url", ""),
                                     "status": ev.get("status", ""),
                                     "note": ev.get("note", "")})
            _crawl["events"] = _crawl["events"][-150:]  # cap the timeline

    try:
        stats = runner.run_crawl(store, url, prompt, states, max_pages, max_depth,
                                 on_event, model, max_extracts, target_records)
        n = stats.get("records", 0)
        cols = stats.get("columns", [])
        dp = _crawl["data_pages"]
        msg = (f"Discovery complete — {dp} data page(s), {n} record(s) extracted "
               f"into {len(cols)} columns.")
        with _crawl_lock:
            _crawl.update(state="done", msg=msg, error=None)
    except Exception as exc:  # noqa: BLE001
        full, where = _error_detail(exc)
        with _crawl_lock:
            _crawl.update(state="error", error=str(exc), msg="",
                          trace=full, where=where)
    finally:
        store.close()


def _export_files() -> list[str]:
    exp = config.DATA_DIR / "exports"
    if not exp.exists():
        return []
    files = sorted(exp.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
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
            if ff_run:
                ff_columns = json.loads(ff_run.get("columns_json") or "[]")
                ff_rows = ff.records_for_run(ff_run["id"])
                ff_stats = json.loads(ff_run.get("stats_json") or "{}")
            ff_runs = []
            for r in ff.list_runs(20):
                rstats = json.loads(r.get("stats_json") or "{}")
                ff_runs.append({
                    "id": r["id"], "seed": r.get("seed"), "prompt": r.get("prompt"),
                    "started": r.get("started"),
                    "rows": ff.count_for_run(r["id"]),
                    "columns": [h for h, _ in json.loads(r.get("columns_json") or "[]")],
                    "diagnosis": rstats.get("diagnosis", ""),
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
            _crawl.update(state="running", msg="", error=None, url=url, prompt=prompt,
                          events=[], fetched=0, data_pages=0, plan=None, llm_calls=0,
                          trace=None, where=None)

        threading.Thread(
            target=_run_crawl_job,
            args=(url, prompt, states, max_pages, 3, model, max_extracts, target_records),
            daemon=True,
        ).start()
        return jsonify(ok=True, url=url, states=states, model=model)

    @app.route("/crawl-status")
    def crawl_status():
        with _crawl_lock:
            job = dict(_crawl)
            job["events"] = list(_crawl["events"])
        return jsonify(job)

    @app.route("/crawl-ack", methods=["POST"])
    def crawl_ack():
        with _crawl_lock:
            if _crawl["state"] in ("done", "error"):
                _crawl.update(state="idle", msg="", error=None, trace=None, where=None)
        return jsonify(ok=True)

    @app.route("/reset", methods=["POST"])
    def reset():
        ff = _ff_store()
        try:
            ff.reset()
        finally:
            ff.close()
        with _crawl_lock:
            _crawl.update(state="idle", msg="", error=None, events=[], plan=None,
                          fetched=0, data_pages=0, llm_calls=0, trace=None, where=None)
        return jsonify(ok=True, msg="All crawl data cleared.")

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
