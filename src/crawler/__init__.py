"""The LLM discovery crawler (the whole product since 2026-06-04).

Given a seed URL and a natural-language prompt, a local LLM (Ollama / gemma4)
plans a crawl intent, then a polite same-domain best-first crawl finds the pages
that carry the target records; ``FreeFormExtractor`` turns each one into rows
keyed by the prompt's fields, stored per-run (``store.py``) and exported with
dynamic columns (``exporters/dynamic_exporter.py``). Orchestrated by
``src.main.crawl()``.

Fetching goes through ``sources.base.ScraplingFetcher`` (stealth mode default):
per-domain rate limiting, bounded retries, and block/CAPTCHA-page detection are
applied to every URL; the robots gate is currently OFF ("Direct Mode" in
``utils.compliance``). Block pages are reported in the run's trace, not solved.
"""

from __future__ import annotations
