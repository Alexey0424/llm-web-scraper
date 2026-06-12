"""Offline tests for the main crawl pipeline (no network, no LLM server).

Covers the three pipeline-level accuracy fixes:
- per-run dedup uses the canonical record key (email -> phone -> name), so the
  same person under two name spellings is stored once;
- a record whose fields never mention the requested segment still counts when
  its source URL proves the segment (detail pages rarely repeat the state);
- standard mode reuses the HTML fetched during discovery instead of paying a
  second fetch per extracted page.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from src import main as main_mod
from src.crawler.store import FreeFormStore
from src.sources.base import FetchResult

BASE = "https://site.test"

_LD = """<script type="application/ld+json">
{{"@type": "Person", "name": "{name}", "telephone": "973-555-0101"}}
</script>"""


class MapFetcher:
    engine = "map"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.gets: list[str] = []

    def get(self, url: str) -> FetchResult:
        p = urlparse(url)
        key = p.path + (f"?{p.query}" if p.query else "")
        self.gets.append(key)
        html = self.pages.get(key)
        if html is None:
            return FetchResult(url=url, content="", ok=False, reason="404 not found")
        return FetchResult(url=url, content=html, ok=True)


@pytest.fixture
def site(monkeypatch, tmp_path):
    """Two NJ detail pages describing the SAME person (same phone, name
    variants), reachable from the seed. No field value ever says 'NJ'."""
    pages = {
        "/": ('<html><body><a href="/agents-nj/jane">Jane Smith</a>'
              '<a href="/agents-nj/jsmith">Jane M Smith</a></body></html>'),
        "/agents-nj/jane": f"<html><head>{_LD.format(name='Jane Smith')}</head>"
                           "<body><h1>Jane Smith</h1></body></html>",
        "/agents-nj/jsmith": f"<html><head>{_LD.format(name='Jane M Smith')}</head>"
                             "<body><h1>Jane M Smith</h1></body></html>",
    }
    fetcher = MapFetcher(pages)
    import src.crawler.discovery as disco
    import src.sources.base as sources
    from src.crawler.llm import OllamaClient

    monkeypatch.setattr(disco, "make_fetcher", lambda *a, **k: fetcher)
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: fetcher)
    monkeypatch.setattr(OllamaClient, "available", lambda self: False)
    monkeypatch.setattr(main_mod.config, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    store = FreeFormStore(tmp_path / "ff.sqlite")
    yield fetcher, store
    store.close()


def _crawl(store, **kw):
    return main_mod.crawl(
        f"{BASE}/", "collect agents: full name, email, phone", ["NJ"],
        extract=True, export_after=False, store=store, max_pages=10, **kw)


def test_segment_matched_from_source_url_and_person_deduped_by_phone(site):
    fetcher, store = site
    out = _crawl(store)
    stats = out["extract_stats"]
    # URL proves NJ -> the record is kept, not dropped off-target...
    assert stats["records"] == 1
    assert stats["dropped_offtarget"] == 0
    # ...and the second spelling of the same person (same phone) deduped.
    rows = store.records_for_run(out["run_id"])
    assert len(rows) == 1


def test_standard_mode_does_not_refetch_extracted_pages(site):
    fetcher, store = site
    _crawl(store)
    for path in ("/agents-nj/jane", "/agents-nj/jsmith"):
        assert fetcher.gets.count(path) == 1, f"{path} fetched more than once"


def test_successful_crawl_persists_timeline(site):
    fetcher, store = site
    out = _crawl(store)
    run = store.get_run(out["run_id"])
    import json
    tl = json.loads(run["timeline_json"])
    assert tl is not None and tl["truncated"] is False
    assert tl["total"] >= 1
    # Extraction emits a timed "extract" event into the stream...
    assert any(e["kind"] == "extract" for e in tl["events"])
    # ...and the crawler-side wiring (DiscoveryCrawler on_event=_record) also
    # captured at least one non-extract (fetch/visit) event.
    assert any(e["kind"] != "extract" for e in tl["events"])
