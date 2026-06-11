# How the crawl behaves — current posture ("Direct Mode")

> **Status note (updated 2026-06-11).** This document used to describe the
> project's original strict-compliance posture (robots.txt obeyed, plain httpx,
> self-identifying User-Agent, stealth disabled). That posture was **changed by
> the project owner in the "Vanilla baseline" commit (2026-06-04)**: the fetcher
> is now Scrapling's browser engine (stealth mode by default) and the robots.txt
> gate is switched off ("Direct Mode"). This rewrite describes — accurately —
> what the code does today, what safeguards remain, and what was removed.
> The old posture is preserved in git history (tag `vanilla` and earlier).

The one-sentence summary of the current behavior:

> **The tool fetches publicly reachable pages the way a regular browser would
> (Scrapling, stealth mode by default, Chrome-like User-Agent), does NOT check
> robots.txt, stays polite (4–6 s per-domain delay, bounded retries, bounded
> scope, same-domain), never logs in or pays to get behind a wall, never solves
> CAPTCHAs, uses no proxies, and records every fetch outcome so a blocked or
> empty crawl is reported honestly instead of silently retried.**

File references use the form `path:line` (line numbers approximate).

---

## 1. Transport: a browser-like fetch, stealth by default

`src/sources/base.py` — `ScraplingFetcher` is the only fetch engine. There is no
httpx fallback. Three modes, selected with the `SCRAPER_MODE` env var:

- `fast` — Scrapling `Fetcher.get(impersonate="chrome", stealthy_headers=True)`:
  static HTTP with browser-like TLS/headers, no JS rendering.
- `dynamic` — `DynamicFetcher.fetch(headless=True, network_idle=True)`: real
  browser rendering for JS-heavy pages.
- `stealth` — **the default** — `StealthyFetcher.fetch(...)`: browser rendering
  with Scrapling's stealth features.

The User-Agent (`config/rate_limits.yaml`) is a standard Chrome UA string, i.e.
the crawler presents itself as a regular browser rather than identifying itself
as a bot.

**What this means plainly:** the tool no longer tries to be recognizable as a
crawler. It reads pages the way a person's browser would.

## 2. robots.txt: currently NOT checked ("Direct Mode")

`src/utils/compliance.py::gate_url` returns *allowed* for every http(s) URL with
the reason string `"Direct Mode: Bypassing robots.txt"`. The robots evaluation
module (`src/utils/robots.py`) still exists and works, but nothing calls it.

To restore robots enforcement, point `gate_url` back at it:

```python
from . import robots
def gate_url(url, user_agent):
    if not robots.is_allowed(url, user_agent):
        return GateResult(False, f"BLOCKED by robots.txt: {url}")
    return GateResult(True, "robots.txt allows")
```

`diagnose()` (`src/crawler/discovery.py`) still distinguishes robots blocks from
anti-bot blocks in its reporting, so re-enabling the gate needs no other change.

## 3. What is still enforced, in code

These safeguards were kept and are active on every fetch:

- **Politeness / rate limiting** — `src/utils/rate_limit.py`: a randomized
  4–6 s delay between requests to the same domain (`config/rate_limits.yaml`),
  single-threaded crawling. The crawler never hammers a server.
- **Bounded retries** — `src/utils/retry.py`: at most 3 retries with
  exponential backoff. Transient-error courtesy, not a battering loop.
- **Bounded scope** — `maxVisits` / `max_depth` / `maxExtracts` caps
  (`src/crawler/discovery.py`, `src/main.py`), or an explicit record target
  that stops the crawl as soon as ~N records are collected. A run takes a
  little, from a few pages — not a mass harvest.
- **Same-domain restriction** — `src/crawler/frontier.py::same_site`: the crawl
  stays on the seed's site unless `--cross-domain` is passed explicitly.
- **Block pages are detected and REPORTED, not defeated** —
  `src/sources/base.py::detect_blocked_page` scans the visible text of each
  response for CAPTCHA / "access denied" / anti-bot interstitials and marks the
  page failed with the reason. There is **no CAPTCHA solver** and no
  challenge-defeat code in the project; a challenge page ends that URL's story
  and the UI says so (`diagnose()`).
- **No login / paywall circumvention** — the crawler sends no credentials and
  has no cookie/session machinery; only publicly reachable pages are read.
- **No proxies** — there is no proxy pool, rotation, or anonymizing relay
  anywhere in the code, the dependencies, or `docker-compose.yml`. All traffic
  comes from your own machine, attributable to you.
- **Local LLM only** — page content is processed by a local Ollama model on the
  internal compose network; nothing is sent to a third-party AI API.

## 4. Transparency and audit trail (unchanged)

- Every fetch outcome (ok / blocked / error, with reason) is recorded in the
  run's `visited` list and shown per-step in the UI's Trace view.
- A manifest is written per crawl to `runs/crawl_<timestamp>.json` (seed,
  prompt, intent, stats, every visited URL + outcome).
- `diagnose()` turns the outcomes into a plain-language explanation in the UI
  ("N page(s) answered with an anti-bot/CAPTCHA block page, which this tool
  does not bypass", "the site returned 403 Forbidden", timeouts, etc.).
- The **Flow** tab in the UI shows the real pipeline, including the fact that
  the robots gate is currently off.

## 5. What changed vs. the original posture — at a glance

| Control | Original (pre 2026-06-04) | Now (Direct Mode) |
|---|---|---|
| robots.txt | obeyed before every fetch | **not checked** |
| Transport | plain `httpx` | Scrapling browser fetch, **stealth default** |
| User-Agent | self-identifying bot UA | Chrome-like browser UA |
| Rate limiting | 2 s fixed/domain | 4–6 s randomized/domain (kept, gentler) |
| Bounded retries / scope / same-domain | yes | yes (kept) |
| CAPTCHA / challenge defeat | none | **still none** — detected and reported |
| Login/paywall circumvention | none | still none |
| Proxies | none | still none |
| Audit trail + honest diagnosis | yes | yes (kept) |

## 6. The owner's responsibility note

With robots.txt off and a browser-like fetch, whether a given crawl is
appropriate is a judgment the operator makes per site (terms of service,
applicable law, the nature of the data). The tool's remaining safeguards keep
each run small, slow, attributable, on-site, and fully logged — but they do not
substitute for that judgment. The previous strict posture can be restored with
the snippet in §2 plus setting `SCRAPER_MODE=fast` and a self-identifying
User-Agent in `config/rate_limits.yaml`.
