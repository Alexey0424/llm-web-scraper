"""Run helper for the web UI — the free-form discovery crawl."""

from __future__ import annotations


def run_crawl(ff_store, url: str, prompt: str, states: list[str],
              max_pages: int = 25, max_depth: int = 3, on_event=None,
              model: str | None = None, max_extracts: int = 10,
              target_records: int | None = None) -> dict:
    """Free-form discovery + extraction (FetchFox-style) for the UI.

    Writes results into a per-run free-form store (dynamic columns from the
    prompt) and exports a dynamic Excel/CSV/JSONL. The UI then renders the latest
    run's columns + rows.
    """
    from ..main import crawl as crawl_pipeline

    out = crawl_pipeline(
        url=url, prompt=prompt, states=states,
        max_pages=max_pages, max_depth=max_depth, max_extracts=max_extracts,
        extract=True, export_after=True, store=ff_store, on_event=on_event,
        model=model, target_records=target_records,
    )
    return out.get("extract_stats") or {"records": 0, "data_pages": 0,
                                        "run_id": None,
                                        "stats": out.get("stats", {})}
