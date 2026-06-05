# Phase 1 — LLM-guided discovery crawler (free-form)

Give the tool a **seed URL** and a **plain-language prompt**; a local LLM
(Ollama / Gemma 4) figures out what you're looking for, crawls the site to find
the pages that list it, and extracts the records. This is the FetchFox-style loop
— run entirely on your own GPU, with no external API.

**Free-form columns.** There is no fixed template: the **fields you name in the
prompt become the Excel/CSV columns**, in order, plus an appended `Source URL`.
Ask for "full name, email, brokerage, city" → you get exactly those columns; ask
for something else → you get that instead. The columns are derived from the
prompt by `src/crawler/fields.py`, extracted as dicts by
`src/crawler/freeform.py`, stored per-run in `FreeFormStore`
(`src/crawler/store.py`), and written by `src/exporters/dynamic_exporter.py`.

## What it does (the FetchFox loop, mapped)

| FetchFox trace | Here |
|----------------|------|
| first `runCompletion` | **Planner** — prompt → structured crawl intent (entity, fields, states, link keywords) |
| many short `runCompletion` | **LinkClassifier / PageClassifier** — score links, judge whether each page is a roster |
| `visit` | compliance-gated fetch (robots + no-bypass + rate limit, reused from the pipeline) |
| `crawl` | **DiscoveryCrawler** — best-first, same-domain BFS over the frontier |
| `agent` / extraction | **FreeFormExtractor** — page HTML → dict rows keyed by the prompt's fields |

The discovered pages are extracted by the LLM into rows keyed by the
prompt-derived columns, de-duplicated, stored per-run in **`FreeFormStore`**
(`src/crawler/store.py`), and written to a **dynamic** Excel/CSV/JSONL
(`src/exporters/dynamic_exporter.py`) whose columns match the prompt.

## Using it

### Web UI (http://localhost:5000) — the **Crawler** tab
Paste a URL, type what you want + the fields, pick a model, set Max visits / Max
extracts, click **Discover agents**. A live **Plan** + per-call **timeline** shows
each LLM call and page visit; when it finishes, the **Discovered records** table
renders (columns from your prompt) and exports to Excel. Past crawls are
revisitable from **Recent crawls**. (The **Flow** tab is a live diagram of the
pipeline.)

### CLI
```bash
# Discovery only (prints the roster URLs it found + a manifest in runs/):
docker compose run --rm scraper crawl \
  --url https://www.example-realty.com --prompt "real estate agents in NJ" --states NJ

# Discover + extract + export to Excel in one shot:
docker compose run --rm scraper crawl \
  --url https://www.example-realty.com \
  --prompt "agents in NJ PA IN: full name, email, phone, agency, city, state" \
  --states NJ PA IN --max-pages 40 --export
```
Flags: `--max-pages` (default 40), `--max-depth` (default 3),
`--cross-domain` (follow off-site links), `--extract`, `--export`.

Every crawl writes a JSON manifest to `runs/crawl_<timestamp>.json` (intent,
stats, discovered pages, every visit + outcome) for auditing.

## Compliance — important

The crawler adds **no new network policy**. Every fetch goes through the same
gate as the rest of the project: it honors `robots.txt` and **never bypasses**
CAPTCHA, anti-bot, login walls or paywalls. You can point it at any URL; sites
that block honest clients simply return *blocked/error* and yield nothing.

> **Zillow (your FetchFox test target):** its `robots.txt` allows the homepage,
> but the server returns **403 Forbidden** (PerimeterX anti-bot) to a truthful
> client. FetchFox got 40 Zillow URLs because it *defeats* anti-bot. This crawler
> will not, by design — it retries politely, logs the 403, and stops. It shines
> on compliant sources (brokerage rosters that publish agent contacts and don't
> block bots).

## The local LLM

- Server: an isolated **Ollama** container (compose service `ollama`, GPU,
  host port **11435** to avoid colliding with any other Ollama on 11434).
- Default model: **`gemma4:26b`** (set in `.env`). `gemma4:e4b` (9.6 GB) is a
  lighter fallback; both are pulled. On a tight-VRAM box see
  `docs/wslconfig-for-gemma4-26b.txt`.
- Swap models any time with `OLLAMA_MODEL` in `.env`, or per-run via the UI model
  dropdown.

If the LLM server is unreachable, the crawler degrades to **heuristic** link/page
scoring (keyword-based) so discovery still runs; only the per-record extraction
needs the model. This is also why the unit tests run with no LLM and no network.

## Code map

```
src/crawler/
  llm.py          OllamaClient (JSON-mode chat, plain generate, health check, timing)
  intent.py       CrawlIntent + Planner (prompt -> structured intent)
  fields.py       derive_columns / dedup_key (prompt fields -> table columns)
  frontier.py     priority frontier + same-domain link extraction
  classifier.py   LinkClassifier (rank links) + PageClassifier (is-listing?)
  discovery.py    DiscoveryCrawler — the crawl loop + diagnose() + live on_event hook
  freeform.py     FreeFormExtractor — page HTML -> dict rows keyed by the columns
  parsing.py      Scrapling Selector helpers (links/text), bs4 fallback
  store.py        FreeFormStore (per-run columns + rows in SQLite)
src/extractors/llm_extractor.py   shared HTML->text helpers (html_to_signal_text, _chunks)
src/exporters/dynamic_exporter.py  dynamic Excel/CSV/JSONL writer
src/sources/base.py               compliance-gated fetcher (robots + rate-limit + retry)
```
Orchestrated by `main.crawl()` / the `crawl` CLI subcommand. UI: `/crawl`,
`/crawl-status`, `/crawl-ack`, `/crawl-models`, `/reset`, `/download` in
`src/web/app.py` + the Crawler & Flow tabs in `templates/index.html`.

Tests: `tests/test_crawler.py` + `tests/test_freeform.py` (offline, no LLM).
End-to-end demo against a local fixture site with the real LLM:
`scripts/crawl_fixture_demo.py`.
