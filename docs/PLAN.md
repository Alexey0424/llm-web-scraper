# Plan

> **Partially superseded (2026-06-11):** the fetch-policy statements below
> ("compliance-gated", "robots-gated, no bypass, plain httpx, no stealth") date
> from the original posture and **no longer match the code**. Since the
> 2026-06-04 "Vanilla baseline" the fetcher is Scrapling (stealth default) and
> the robots gate is off — see `LEGALITY_OF_THE_CRAWL.md` for the current
> account. The pipeline structure described here is still accurate.

The project is a **local-LLM discovery crawler** (the original fixed-schema
real-estate scraping pipeline was removed on 2026-06-04 — only the crawler remains).

## Summary

Given a seed URL + a natural-language prompt, a local LLM (Ollama / Gemma 4) plans
the crawl, walks the site (compliance-gated), finds the pages that list the target
records, and extracts the **prompt-named fields** into a dynamic Excel/CSV/JSONL.

### Pipeline

`prompt + seed URL → plan (LLM) → derive columns → best-first crawl
(robots-gated, polite, no bypass) → classify pages → LLM-extract requested fields
→ dedupe/store → dynamic export`

Detailed design: `docs/PHASE1_CRAWLER.md`.

### Key decisions

- **Free-form columns** — the prompt decides the Excel columns; there is no fixed
  template.
- **Local LLM** via Ollama (`gemma4:26b` default, swappable via `OLLAMA_MODEL`).
- **No bypass** — honor `robots.txt`, polite rate limits, plain `httpx` (no
  stealth/fingerprinting); blocked sites are logged with the reason, never
  bypassed. Full account: `LEGALITY_OF_THE_CRAWL.md`.
- **Scrapling** is used as the HTML **parser** (`Selector`), bs4 fallback — not as
  a fetcher (its fetchers pull in stealth deps we reject).
