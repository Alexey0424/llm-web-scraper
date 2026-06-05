"""Crawler entry point.

The single command is ``crawl`` (see cli.py): LLM-guided, free-form discovery —
given a seed URL + a natural-language prompt, plan an intent, crawl the site
(compliance-gated), and extract the prompt's fields into a dynamic Excel/CSV/JSONL.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# crawl (Phase 1: LLM-guided URL discovery -> free-form extract/export)
# --------------------------------------------------------------------------
def crawl(
    url: str,
    prompt: str,
    states: list[str],
    *,
    max_pages: int = 40,
    max_depth: int = 3,
    max_extracts: int = 10,
    same_domain: bool = True,
    extract: bool = False,
    export_after: bool = False,
    store=None,
    on_event=None,
    model: str | None = None,
) -> dict:
    """Free-form discovery + extraction (FetchFox-style).

    Pipeline: plan intent (LLM) -> columns derived from the prompt -> best-first
    crawl (compliance-gated) -> discovered data-page URLs -> LLM extracts exactly
    the requested fields per page -> dedupe/store -> dynamic Excel/CSV/JSONL whose
    columns match the prompt. There is no fixed template; the prompt decides the
    columns. ``store`` is an optional ``FreeFormStore`` (the UI passes one).
    """
    from .crawler.discovery import DiscoveryCrawler, diagnose
    from .crawler.fields import derive_columns, dedup_key
    from .crawler.freeform import FreeFormExtractor
    from .crawler.intent import Planner
    from .crawler.llm import OllamaClient
    from .crawler.store import FreeFormStore, now_iso
    from .exporters import dynamic_exporter
    from .sources.base import make_fetcher

    config.ensure_dirs()
    # `model` (optional, e.g. from the UI toggle) overrides OLLAMA_MODEL for this
    # crawl; the same model is reused for extraction so results are consistent.
    client = OllamaClient(model=model)
    if on_event:
        # Every LLM completion emits a timed "runCompletion" to the live feed.
        client.on_call = on_event
    if not client.available():
        log.warning(
            "Ollama model '%s' not reachable at %s -- crawl will use heuristic "
            "link/page scoring only (no LLM extraction).",
            client.model, client.host,
        )
    intent = Planner(client).plan(prompt, url, states)
    columns = derive_columns(intent.fields)
    log.info("crawl intent: entity=%r columns=%s", intent.entity,
             [h for h, _ in columns])
    if on_event:
        on_event({"kind": "plan", "method": "crawl", "start_url": url,
                  "entity": intent.entity, "query": intent.description or prompt,
                  "fields": [h for h, _ in columns], "max_visits": max_pages,
                  "max_extracts": max_extracts, "max_depth": max_depth,
                  "model": client.model, "proxy": "none (compliant)"})

    crawler = DiscoveryCrawler(
        intent, client, max_pages=max_pages, max_depth=max_depth,
        same_domain=same_domain, on_event=on_event,
    )
    result = crawler.run(url)

    ts = _ts()
    manifest = {
        "seed": url, "prompt": prompt, "states": states,
        "intent": intent.model_dump(), "columns": columns,
        "stats": result.stats,
        "data_pages": [p.__dict__ for p in result.data_pages],
        "visited": result.visited,
    }
    man_path = config.RUNS_DIR / f"crawl_{ts}.json"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nDiscovered {len(result.data_pages)} data page(s) "
          f"(fetched {result.stats['pages_fetched']}, "
          f"blocked {result.stats['blocked']}, errors {result.stats['errors']}, "
          f"LLM calls {result.stats['llm_calls']}). Columns: "
          f"{[h for h, _ in columns]}")
    for p in sorted(result.data_pages, key=lambda x: -x.score):
        print(f"  [{p.score:.2f}] ~{p.estimated_records:>3} recs  {p.url}")
    print(f"\nCrawl manifest: {man_path}")

    out: dict = {"manifest": str(man_path), "stats": result.stats,
                 "columns": columns, "data_page_urls": result.data_page_urls}

    if not extract:
        return out

    # ---- Free-form extraction. The run is ALWAYS recorded (even with zero
    # discovered pages) so every crawl appears in history with its request and
    # outcome -- a blocked/empty site is a result worth keeping, not a no-op. --
    own_store = store is None
    ff = store or FreeFormStore(config.DATA_DIR / "processed" / "freeform.sqlite")
    run_id = ff.start_run(url, prompt, columns, now_iso())
    dk = dedup_key(columns)
    kept = 0
    # maxExtracts: only extract from the top-scoring discovered pages.
    pages_to_extract = result.data_page_urls[:max_extracts]
    if result.data_page_urls:
        if len(result.data_page_urls) > len(pages_to_extract):
            log.info("maxExtracts=%d: extracting %d of %d discovered pages",
                     max_extracts, len(pages_to_extract), len(result.data_page_urls))
        extractor = FreeFormExtractor(columns, entity=intent.entity, client=client)
        fetcher = make_fetcher()
        for page_url in pages_to_extract:
            res = fetcher.get(page_url)
            if not res.ok:
                log.info("extract skip %s (%s)", page_url, res.reason[:80])
                continue
            rows = extractor.extract(res.content, base_url=page_url)
            added = 0
            for row in rows:
                if ff.add_record(run_id, (row.get(dk) or "").lower(), page_url, row, now_iso()):
                    added += 1
                    kept += 1
            if on_event:
                on_event({"kind": "extract", "url": page_url, "status": "extracted",
                          "fetched": 0, "data_pages": len(result.data_pages),
                          "note": f"+{added} record(s) ({kept} total)"})
    else:
        log.info("no data pages discovered; recording crawl with 0 records")

    ex_stats = {"records": kept, "data_pages": len(result.data_pages),
                "extracted_pages": len(pages_to_extract),
                "pages_fetched": result.stats.get("pages_fetched", 0),
                "blocked": result.stats.get("blocked", 0),
                "errors": result.stats.get("errors", 0),
                "columns": [h for h, _ in columns], "run_id": run_id,
                "llm_calls": client.calls,
                # Why a crawl found nothing, + the per-URL outcomes behind it.
                "diagnosis": diagnose(result),
                "visited": result.visited[:30]}
    ff.finish_run(run_id, now_iso(), ex_stats)
    out["extract_stats"] = ex_stats
    out["run_id"] = run_id
    print(f"\nExtraction: {kept} record(s) into {len(columns)} columns.")

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

    if own_store:
        ff.close()
    return out


if __name__ == "__main__":
    from .cli import main as _cli_main

    raise SystemExit(_cli_main())
