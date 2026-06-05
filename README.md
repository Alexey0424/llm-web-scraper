# Discovery Crawler (LLM-guided, free-form)

Give the tool a **seed URL** and a **plain-language prompt** naming the fields you
want; a **local LLM** (Ollama / Gemma 4, on your own GPU) plans the crawl, walks
the site to find the pages that list those records, and extracts them into an
Excel/CSV/JSONL whose **columns come from your prompt**. A FetchFox-style loop,
run entirely locally — no external API.

> **Compliance notice.** This tool collects only **publicly available**
> information. It honors `robots.txt` and polite per-domain rate limits, and it
> **does not** bypass CAPTCHA, anti-bot protection, login walls, or paywalls, nor
> impersonate browsers (no stealth/fingerprinting). Sites that refuse an honest
> client are logged with the reason, never bypassed. The full, code-level account
> is in **`LEGALITY_OF_THE_CRAWL.md`**.

## How it works

`prompt + seed URL → plan (LLM) → derive columns → best-first crawl (compliance-gated)
→ classify pages → LLM-extract the requested fields → dedupe/store → dynamic export`

- **Free-form columns** — the fields you name in the prompt *are* the columns
  (plus an appended `Source URL`). No fixed template.
- **Local LLM** via Ollama (`gemma4:26b` default, swappable with `OLLAMA_MODEL`).
- **Compliant fetching** — plain `httpx`, robots-gated, rate-limited, no bypass.
- **Scrapling** is used as the resilient HTML **parser** (`Selector`), with
  BeautifulSoup as fallback.

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up -d ollama          # local GPU LLM (one-time model pull)
docker compose up ui                 # open http://localhost:5000  → "Crawler" tab
```

In the **Crawler** tab: paste a URL, type what you want + the fields, pick a model,
set Max visits / Max extracts, click **Discover agents**. Watch the live Plan +
per-call timeline; the discovered records render below and export to Excel. Past
crawls are revisitable from **Recent crawls**. The **Flow** tab is a live diagram
of the pipeline.

### CLI

```bash
docker compose run --rm scraper crawl \
  --url https://www.example.com \
  --prompt "agents: full name, email, phone, agency, city" \
  --max-pages 40 --max-extracts 10 --extract --export
```

## Layout

```
src/crawler/        the crawler (intent, frontier, classifier, discovery,
                    freeform extractor, parsing, store, glue, llm client)
src/sources/base.py compliance-gated fetcher (robots + rate-limit + retry)
src/extractors/     dynamic_exporter (xlsx/csv/jsonl) + shared HTML helpers
src/web/            Flask UI (Crawler + Flow tabs)
config/rate_limits.yaml   polite-fetch settings (UA, delay, retries)
```

See **`docs/PHASE1_CRAWLER.md`** for the detailed design and **`LEGALITY_OF_THE_CRAWL.md`**
for the compliance/ethics account with code references.
