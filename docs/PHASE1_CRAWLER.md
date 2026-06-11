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
| `visit` | Scrapling browser fetch (stealth default; rate-limited, bounded retries, block-page detection; robots gate off — Direct Mode) |
| `crawl` | **DiscoveryCrawler** — best-first, same-domain BFS over the frontier |
| `agent` / extraction | **FreeFormExtractor** — page HTML → dict rows keyed by the prompt's fields |

The discovered pages are extracted by the LLM into rows keyed by the
prompt-derived columns, de-duplicated, stored per-run in **`FreeFormStore`**
(`src/crawler/store.py`), and written to a **dynamic** Excel/CSV/JSONL
(`src/exporters/dynamic_exporter.py`) whose columns match the prompt.

## Using it

### Web UI (http://localhost:5000) — the **Crawler** tab
Paste a URL, type what you want + the fields, pick a model, set Max visits / Max
extracts, click **Start discovery**. A live **Plan** + per-call **timeline** shows
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

## Fetch behavior — current posture (Direct Mode)

> Updated 2026-06-11 to match the code. The original strict posture (robots
> obeyed, plain httpx, no stealth) was replaced in the 2026-06-04 "Vanilla
> baseline"; the account below is the current truth. Details + how to restore
> the old posture: `LEGALITY_OF_THE_CRAWL.md`.

Every fetch goes through `src/sources/base.py`: Scrapling browser fetch
(`SCRAPER_MODE`: `fast` | `dynamic` | `stealth`, default **stealth**) with a
per-domain randomized 4–6 s delay, bounded exponential-backoff retries, and a
same-domain frontier. The `robots.txt` gate is currently **off** ("Direct Mode"
in `src/utils/compliance.py`). There is still **no** CAPTCHA solving, no
login/paywall circumvention, and no proxying: a challenge/block page is detected
from its visible text (`detect_blocked_page`), marked failed with the reason,
and reported in the UI's diagnosis — never solved.

## The local LLM

- Server: an isolated **Ollama** container (compose service `ollama`, GPU,
  host port **11435** to avoid colliding with any other Ollama on 11434).
- Default model: **`gemma4:e4b`** (set in `.env` and `crawler/llm.py`; the edge
  variant — fits in VRAM alone). `gemma4:26b` is the higher-quality option on a
  roomy box; see `docs/wslconfig-for-gemma4-26b.txt`.
- Swap models any time with `OLLAMA_MODEL` in `.env`, or per-run via the UI model
  dropdown.

If the LLM server is unreachable, the crawler degrades to **heuristic** link/page
scoring (keyword-based) so discovery still runs; only the per-record extraction
needs the model. This is also why the unit tests run with no LLM and no network.

## Code map

```
src/crawler/
  llm.py          OllamaClient (schema-constrained JSON chat + retry, health check, timing)
  intent.py       CrawlIntent + Planner (prompt -> structured intent; prompt-derived keywords)
  fields.py       derive_columns / dedup_key (prompt fields -> table columns)
  frontier.py     priority frontier + canonicalized same-domain link extraction
  classifier.py   LinkClassifier (rank links) + PageClassifier (is-listing?)
  discovery.py    DiscoveryCrawler — crawl loop, detail-pattern validation,
                  pagination following, diagnose(), live on_event hook
  sitemap.py      /sitemap.xml seeding (bounded, same-site, rate-limited)
  freeform.py     FreeFormExtractor — rows keyed by the columns; grounding guard;
                  derived name fields; JSON-LD merge
  jsonld.py       schema.org JSON-LD reader (name/phone/email/address/worksFor)
  dedup.py        canonical record key (email -> phone -> name+location) + export ledger
  parsing.py      Scrapling Selector helpers (links/text/rel=next), bs4 fallback
  store.py        FreeFormStore (per-run columns + rows in SQLite)
src/extractors/llm_extractor.py   HTML -> line-structured text (+ [email:]/[phone:] markers)
src/exporters/dynamic_exporter.py  dynamic Excel/CSV/JSONL writer
src/sources/base.py               Scrapling fetcher (rate-limit + retry + block-page detection)
```
Orchestrated by `main.crawl()` / the `crawl` CLI subcommand. UI: `/crawl`,
`/crawl-status`, `/crawl-ack`, `/crawl-models`, `/reset`, `/download` in
`src/web/app.py` + the Crawler & Flow tabs in `templates/index.html`.

Tests: `tests/` (offline, no LLM — frontier, classifiers, grounding, derived
fields, sitemap, pagination, detail-pattern validation, main pipeline).
End-to-end demo against a local fixture site with the real LLM:
`scripts/crawl_fixture_demo.py`. Accuracy benchmark with golden records:
`scripts/bench_extraction.py` (live model; per-field precision/recall).
