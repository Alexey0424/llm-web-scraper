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
    target_records: int | None = None,
    batch_id: str | None = None,
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

    # Record-target mode: when the caller asks for ~N records, extraction is
    # interleaved into discovery (via on_data_page) so the crawl can STOP as soon
    # as the target is met -- reusing the already-fetched HTML. maxVisits /
    # maxExtracts are then ignored (the UI greys them out); a generous page cap
    # still bounds the crawl so it can't run forever.
    target = target_records if (extract and target_records and target_records > 0) else None

    if on_event:
        on_event({"kind": "plan", "method": "crawl", "start_url": url,
                  "entity": intent.entity, "query": intent.description or prompt,
                  "fields": [h for h, _ in columns],
                  "max_visits": ("∞ (until target)" if target else max_pages),
                  "max_extracts": (f"until {target} records" if target else max_extracts),
                  "max_depth": max_depth, "target_records": target,
                  "model": client.model, "proxy": "none (compliant)"})

    # Generic "target segments" (the entity-agnostic generalization of states):
    # the distinct values the user wants results spread across. When present, the
    # pipeline (a) keeps ONLY records matching a requested segment and (b) in
    # target mode, balances the record budget across them so no one segment hogs
    # the result. Empty => no filtering/balancing (behaves like a plain crawl).
    segs = [s.strip() for s in (intent.segments or intent.states) if s and s.strip()]
    per_seg = (-(-target // len(segs)) if (target is not None and segs) else None)  # ceil
    seg_counts: dict[str, int] = {s: 0 for s in segs}

    def _match_segment(row: dict) -> str | None:
        """Which requested segment this record belongs to (None = off-target).

        Whole-word match of a segment value against the record's field values, so
        short codes (``IN``) don't match unrelated text (``Indianapolis``). Returns
        "" when no segments were requested (everything passes)."""
        if not segs:
            return ""
        hay = " ".join(str(v) for v in row.values() if v)
        for s in segs:
            if re.search(r"\b" + re.escape(s) + r"\b", hay, re.IGNORECASE):
                return s
        return None

    # Extraction state is set up BEFORE the crawl so the discovery callback (target
    # mode) can use it. The run is ALWAYS recorded (even with zero discovered
    # pages) when extract=True, so every crawl appears in history.
    own_store = (store is None) if extract else False
    ff = (store or FreeFormStore(config.DATA_DIR / "processed" / "freeform.sqlite")) if extract else None
    run_id = ff.start_run(url, prompt, columns, now_iso(), batch_id=batch_id) if ff else None
    dk = dedup_key(columns) if ff else None
    extractor = FreeFormExtractor(columns, entity=intent.entity, client=client) if ff else None
    kept = 0
    dropped_offtarget = 0
    extracted_urls: list[str] = []
    dp_count = 0

    def _extract_page(page_url: str, html: str) -> int:
        nonlocal kept, dp_count, dropped_offtarget
        dp_count += 1
        added = 0
        for row in extractor.extract(html, base_url=page_url):
            if target is not None and kept >= target:
                break  # hit the overall record target -> stop mid-page, no overshoot
            seg = _match_segment(row)
            if seg is None:
                dropped_offtarget += 1  # not in any requested segment -> drop
                continue
            if per_seg is not None and seg and seg_counts.get(seg, 0) >= per_seg:
                continue  # this segment's fair share is full -> keep results balanced
            if ff.add_record(run_id, (row.get(dk) or "").lower(), page_url, row, now_iso()):
                added += 1
                kept += 1
                if seg:
                    seg_counts[seg] = seg_counts.get(seg, 0) + 1
        extracted_urls.append(page_url)
        if on_event:
            on_event({"kind": "extract", "url": page_url, "status": "extracted",
                      "fetched": 0, "data_pages": dp_count, "records": kept,
                      "note": f"+{added} record(s) ({kept} total)"})
        return added

    def _target_met() -> bool:
        # Stop at the overall target; per-segment caps (per_seg) keep it balanced
        # so no single segment hogs the budget. This honors a small target like 5
        # (which can't divide evenly across the segments) without overshooting.
        return target is not None and kept >= target

    def _needed_segments() -> set:
        # Segments still under their per-segment cap. With no cap (no target) all
        # segments are "needed" -> the crawl skips nothing.
        if per_seg is None:
            return set(segs)
        return {s for s in segs if seg_counts.get(s, 0) < per_seg}

    on_data_page = None
    if target is not None:
        def on_data_page(page_url, html, verdict):  # noqa: ANN001 - simple callback
            _extract_page(page_url, html)
            return not _target_met()  # returning False stops the crawl
        # Safety cap: the callback stops early once balanced; this only bounds the
        # worst case where some segment can never be filled.
        max_pages = max(max_pages, 40)

    crawler = DiscoveryCrawler(
        intent, client, max_pages=max_pages, max_depth=max_depth,
        same_domain=same_domain, on_event=on_event, on_data_page=on_data_page,
        needed_segments=(_needed_segments if (target is not None and segs) else None),
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

    if target is None:
        # Standard mode: extract from the most COMPLETE pages first (#2) -- those
        # where the classifier saw the most requested fields -- ties broken by
        # crawl score. Re-fetch each (discovery didn't retain their HTML). Target
        # mode already extracted during discovery via the callback.
        if result.data_page_urls:
            ranked = sorted(result.data_pages,
                            key=lambda p: (len(p.fields_found), p.score), reverse=True)
            pages_to_extract = [p.url for p in ranked][:max_extracts]
            if len(result.data_page_urls) > len(pages_to_extract):
                log.info("maxExtracts=%d: extracting %d of %d discovered pages",
                         max_extracts, len(pages_to_extract), len(result.data_page_urls))
            fetcher = make_fetcher()
            for page_url in pages_to_extract:
                res = fetcher.get(page_url)
                if not res.ok:
                    log.info("extract skip %s (%s)", page_url, res.reason[:80])
                    continue
                _extract_page(page_url, res.content)
        else:
            log.info("no data pages discovered; recording crawl with 0 records")
    elif _target_met():
        log.info("record target met: %d records%s; crawl stopped early", kept,
                 (" (balanced " + ", ".join(f"{s}:{seg_counts[s]}" for s in segs) + ")") if segs else "")
    else:
        log.info("record target not met: %d/%d records%s (crawl exhausted)", kept, target,
                 (" — per-segment " + ", ".join(f"{s}:{seg_counts[s]}" for s in segs)) if segs else "")

    # Field coverage (#2): which requested fields were ever seen on a data page,
    # and which were never found anywhere -- so the user learns up front that a
    # source simply doesn't publish (e.g.) email/phone, instead of guessing.
    seen_fields = set()
    for p in result.data_pages:
        seen_fields.update(f.strip().lower() for f in p.fields_found)
    requested_headers = [h for h, k in columns if k != "source_url"]
    missing_fields = [h for h in requested_headers if h.lower() not in seen_fields]

    ex_stats = {"records": kept, "data_pages": len(result.data_pages),
                "extracted_pages": len(extracted_urls),
                "target_records": target,
                "target_met": bool(target is not None and _target_met()),
                "segment_kind": intent.segment_kind,
                "segment_counts": dict(seg_counts),
                "dropped_offtarget": dropped_offtarget,
                "pages_fetched": result.stats.get("pages_fetched", 0),
                "blocked": result.stats.get("blocked", 0),
                "errors": result.stats.get("errors", 0),
                "columns": [h for h, _ in columns], "run_id": run_id,
                "llm_calls": client.calls,
                # Why a crawl found nothing, + the per-URL outcomes behind it.
                "diagnosis": diagnose(result),
                "field_coverage": {"found": sorted(seen_fields),
                                   "missing": missing_fields},
                "visited": result.visited[:30]}
    if missing_fields:
        log.info("field coverage: %d/%d requested fields never appeared on any "
                 "data page (likely not published by this source): %s",
                 len(missing_fields), len(requested_headers), ", ".join(missing_fields))
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
