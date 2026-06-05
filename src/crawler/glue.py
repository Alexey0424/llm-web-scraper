"""Bridge the discovery crawler to the existing extraction pipeline.

The crawler's output -- a set of discovered data-page URLs grouped by state --
is shaped into a runtime ``source_cfg`` dict, exactly the structure
``Pipeline.run(source_cfgs=[...])`` already accepts (the same mechanism the demo
mode uses). The config declares ``extractor: llm`` so each page is parsed by the
LLM extractor. Nothing downstream (normalize / dedupe / score / export) changes.
"""

from __future__ import annotations

import re

from .discovery import CrawlResult


def slugify(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower().replace("www.", "")
    return "crawl_" + re.sub(r"[^a-z0-9]+", "_", host).strip("_")


def result_to_source_cfg(result: CrawlResult, name: str | None = None) -> dict:
    """Build a single ``extractor: llm`` source config from discovered pages.

    Each discovered page is assigned to every state in the intent (the LLM
    extractor re-reads the real per-record state from the page anyway, and the
    pipeline normalizes/filters by state downstream).
    """
    urls = result.data_page_urls
    states = result.intent.states or ["NJ", "PA", "IN"]
    return {
        "name": name or slugify(result.intent.seed_url),
        "base_url": result.intent.seed_url,
        "states": list(states),
        "enabled": True,
        "render": "static",
        "extractor": "llm",
        "pages": {s: list(urls) for s in states},
        "discovered_from": result.intent.seed_url,
        "prompt": result.intent.description,
    }
