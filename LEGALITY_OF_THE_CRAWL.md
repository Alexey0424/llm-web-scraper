# Parts that ensure the legality & ethics of the crawl

This document traces — **in order, from the moment you press “Discover agents” in
the Crawler tab** — every part of the code that keeps this tool on the right side
of the line: legal, ethical, and respectful of the sites it visits.

The guiding principle is one sentence:

> **We collect only publicly available information, we identify ourselves
> honestly, we obey `robots.txt`, we are polite, we stay in a bounded scope, and
> we NEVER bypass any access control (no CAPTCHA solving, no anti-bot/Cloudflare/
> PerimeterX defeat, no fingerprint spoofing, no login/paywall circumvention, no
> proxy rotation).** If a site refuses an honest client, we stop and log why.

Everything below is *how that sentence is enforced in code*. File references use
the form `path:line`.

---

## 0. The request is honest from the start (no evasion infrastructure)

Before any code runs, note what is **absent** by design: there is no proxy pool,
no stealth browser, no fingerprint randomizer, no CAPTCHA-solver. The crawler
fetches over plain HTTP with an **identifying User-Agent** that states its purpose:

```yaml
# config/rate_limits.yaml:5
user_agent: "RealEstateAgentScraper/0.1 (+compliance: public business data only; respects robots.txt)"
# :3  min_delay_seconds: 2.0      # politeness delay between hits to the same domain
# :6  request_timeout_seconds: 30
# :7  max_retries: 3
# :8  backoff_base_seconds: 1.5
```

A site operator who looks at their logs can see exactly who we are and why. That
is the opposite of evasion.

### 0a. The build files (Docker) exclude evasion tooling at install time

The posture is enforced by *how the container is built*, not just by the runtime
code — these are the files you'd build the image from:

**`requirements.txt`** — installs only honest tooling and explicitly drops
Scrapling's fetcher/stealth extra:

```text
# requirements.txt:11-14
# Scrapling with its `fetchers` extra ... pulls curl_cffi, browserforge, ...
# We use the plain Fetcher only; StealthyFetcher / browser stealth are NOT used.
scrapling>=0.4 #scrapling[fetchers]>=0.4   ← line 14: the [fetchers] extra is COMMENTED OUT
```

- With `[fetchers]` disabled, **`curl_cffi` / `browserforge` (browser-fingerprint
  generation) are never installed** → Scrapling's stealth fetcher cannot run. This
  is the build-time root cause of §4.
- The whole list (`requirements.txt:1-27`) contains **no** proxy-rotation,
  CAPTCHA-solving, or anti-bot/Cloudflare-defeat package — only `httpx`,
  `beautifulsoup4`+`lxml`, `scrapling` (used as the **parser**), `playwright`
  (vanilla), `flask`, `openpyxl`, `pandas`, `pytest`.

**`Dockerfile`** — the only browser it installs is explicitly vanilla:

```dockerfile
# Dockerfile:16-17   pip install -r requirements.txt   (so the above is all that lands)
# Dockerfile:19-23
# Install a Chromium for Playwright + Scrapling's fetcher (vanilla, no stealth).
RUN playwright install --with-deps chromium || echo "playwright browser install skipped"
RUN scrapling install || echo "scrapling browser fetch skipped"
```

Plain Chromium — **no Camoufox / anti-detect browser, no fingerprint patches**.

**`docker-compose.yml`** — everything runs locally; nothing is routed through an
anonymizing proxy:

- The LLM is a **local** `ollama` service (`docker-compose.yml:5-21`), reached only
  on the internal compose network (`OLLAMA_HOST: http://ollama:11434`, lines 29 &
  63) — no third-party AI API, no page content sent off-box.
- `env_file: .env` (lines 26-27 & 60-61) — the default compliant run needs **no API
  keys or secrets**.
- The only published ports are the local UI (`5000`) and the ollama debug port
  (`11435`); there is **no proxy service of any kind** in the stack.

So "no evasion infrastructure" is enforced at three levels: it isn't coded, it
isn't installed, and it isn't even present in the image.

---

## 1. You press “Discover” → `POST /crawl` (input only, no shortcuts)

`src/web/app.py:234-252` — the `/crawl` route reads the URL + prompt, requires
both, and starts a background job. It does **not** add any headers, cookies, or
tricks; it just hands the URL and prompt to the pipeline. The crawl is general
(no hardcoded states) and the location, if any, lives in your prompt.

```python
# src/web/app.py:234-252  (the /crawl route)
@app.route("/crawl", methods=["POST"])                 # 234
def crawl():                                           # 235
    url = (request.form.get("url") or "").strip()      # 236
    prompt = (request.form.get("prompt") or "").strip()
    ...
    if not url or not prompt:
        return jsonify(ok=False, error="A seed URL and a prompt are both required."), 400  # 251
```

The job calls `runner.run_crawl(...)` (`src/web/runner.py`) → `src/main.py::crawl()`
(**`src/main.py:195`**).

---

## 2. The crawler uses the compliance-gated fetcher (not a raw client)

In `src/main.py::crawl()` (**`src/main.py:195`**) the discovery crawler is built
with the project’s **`make_fetcher()`** (**`src/sources/base.py:151`**), which
returns a fetch engine that runs **without stealth**
and that funnels *every* request through the compliance gate (next section):

```python
# src/sources/base.py:151-156  (make_fetcher)
# Default is Scrapling's plain (non-stealth) engine; falls back to httpx.
# Scrapling runs WITHOUT stealth and falls back to httpx automatically.
```

**In practice every fetch is plain `httpx`.** Scrapling’s *fetcher* engines pull
in `curl_cffi` → `browserforge` (browser-**fingerprint** generation) and a browser
stack — i.e. the very anti-detection machinery our policy forbids. Those packages
are deliberately **not installed**, so the Scrapling fetcher can’t initialize and
the code transparently falls back to honest `httpx`. The absence of those deps is
itself a compliance control. (Note: Scrapling is still used heavily — but as the
HTML **parser** (`Selector`), see §5 — not as a stealth fetcher.)

Crucially, the engine choice changes *only the transport* — the legality controls
(robots, rate-limit, retry, honest UA) live in the shared base class and apply no
matter which engine is active.

---

## 3. The heart of it: every single fetch goes through `HttpFetcher.get()`

`src/sources/base.py:68-84`. This method runs for **every URL** the crawler
touches — the seed and every discovered link. Read the order carefully; the gate
comes **first, before any network request**:

```python
# src/sources/base.py:68-84
def get(self, url: str) -> FetchResult:
    gate = compliance.gate_url(url, self.user_agent)   # 69  ← robots.txt check FIRST
    if not gate.allowed:                               # 70
        log.warning(gate.reason)                       # 71
        return FetchResult(url, "", ok=False, reason=gate.reason)  # 72  ← refuse, do NOT fetch
    self.limiter.wait(url)                             # 73  ← polite per-domain delay

    @with_retry(max_retries=self.max_retries, base=self.backoff)  # 75  ← bounded retry
    def _do() -> tuple[int, str]:
        return self._raw_get(url)                      # 77

    try:
        _status, text = _do()                          # 80
    except Exception as exc:                           # 81
        log.warning("fetch failed %s: %s", url, exc)   # 82
        return FetchResult(url, "", ok=False, reason=str(exc))  # 83  ← on failure, stop honestly
    return FetchResult(url, text, ok=True)             # 84
```

### 3a. robots.txt is honored — `gate_url` (the access-control boundary)

```python
# src/utils/compliance.py:36-40
def gate_url(url: str, user_agent: str) -> GateResult:
    """Per-URL robots.txt check, applied immediately before fetching."""
    if not robots.is_allowed(url, user_agent):          # 38
        return GateResult(False, f"BLOCKED by robots.txt: {url}")  # 39  ← disallowed → refuse
    return GateResult(True, f"robots.txt allows: {url}")           # 40
```

The actual robots decision fetches and parses the site’s real `robots.txt` with
the standard-library parser, using **our** user-agent:

```python
# src/utils/robots.py:17-32  (_parser_for — fetch & parse the site's robots.txt)
robots_url = urljoin(base, "/robots.txt")               # 20
resp = httpx.get(robots_url, timeout=15, headers={"User-Agent": user_agent})  # 23
if resp.status_code == 200:
    rp.parse(resp.text.splitlines())                    # 25
else:
    rp.parse([])                                        # 28  ← no robots.txt (404) = "allow all" (convention)

# src/utils/robots.py:35-42  (is_allowed — ask the parser if WE may fetch this URL)
def is_allowed(url: str, user_agent: str) -> bool:
    rp = _parser_for(base, user_agent)
    return rp.can_fetch(user_agent, url)                # 40
```

> **What this means:** if a path is disallowed for our agent, the fetcher returns
> a failure *before opening a connection to that path*. We never request a page
> robots.txt tells us not to.

### 3b. We are polite — per-domain rate limiting

`self.limiter.wait(url)` (base.py:73) blocks until the minimum delay since the
last hit to that domain has elapsed (default **2 s**, configurable per host):

```python
# src/utils/rate_limit.py:31-45  (DomainRateLimiter.wait)
domain = urlparse(url).netloc.lower()                   # 33
delay = self._delay_for(domain)                         # 34  ← default 2.0s, per-domain override allowed
...
if elapsed < delay:
    waited = delay - elapsed
    self._sleep(waited)                                 # 43  ← actually wait before hitting the site again
```

This guarantees we never hammer a server — at most one request every couple of
seconds per domain, single-threaded per domain.

### 3c. We back off, we don’t pound — bounded retry

```python
# src/utils/retry.py:10-39  (with_retry)
for attempt in range(max_retries + 1):                  # 27  ← at most 3 retries
    try:
        return fn(*args, **kwargs)
    except exc_tuple as exc:
        if attempt == max_retries:
            break
        sleep_fn(base ** (attempt + 1))                 # 34  ← exponential backoff (1.5^n s)
raise last_exc
```

Retries are few and spaced out with exponential backoff — this is courtesy for
transient errors, **not** a brute-force loop to wear down a defense.

### 3d. On block or refusal, we stop — we do not escalate

base.py:81-83: any failure (a robots block, a 403, a timeout) returns
`ok=False` with the reason. **There is no “try harder” path** — no switching to a
stealth mode, no proxy, no header spoofing. The crawl simply records what
happened and moves on.

---

## 4. The transport is non-stealth — and Scrapling’s fetcher can’t even start here

`src/sources/base.py:87-148`. Scrapling is the project’s primary tool, but for
**fetching** it is intentionally hobbled — and in this environment it simply
**cannot initialize**, so every request is plain `httpx`:

```python
# src/sources/base.py:88-96 (class docstring — the policy, in code)
"""Scrapling stealth / anti-bot features are deliberately NOT used
   (no StealthyFetcher anti-bot behavior, no Camoufox)."""

# src/sources/base.py:133-148 (_raw_get)
# If the installed Scrapling can't serve a plain request, fall back to httpx
# (line 145) rather than reaching for any evasion. A >=400 status is RAISED
# (line 146-147) so it surfaces honestly as an error — never silently retried-around.
```

**Why it can’t start:** Scrapling’s fetcher engines depend on `curl_cffi` and
`browserforge` (browser-**fingerprint** generation) — exactly the anti-detection
machinery our no-bypass policy rejects. Those packages are **not installed**, so
the fetcher raises on init and `_raw_get` falls back to honest `httpx` (verified:
the missing-dependency chain is `curl_cffi → browserforge`). The result is that
**no stealth, fingerprinting, CAPTCHA-solving, or anti-bot defeat exists anywhere
in the fetch path** — by policy *and* by what is installed.

Scrapling **is** used heavily — just as the HTML **parser** (`Selector`), which
needs none of those deps. See §5a.

---

## 5. The crawl loop applies the gate to EVERY discovered link

`src/crawler/discovery.py`. The best-first loop calls the gated `fetcher.get()`
on every URL it pops — so robots, rate-limit and retry apply to the seed *and*
every link the LLM/heuristics decide to follow:

```python
# src/crawler/discovery.py:93-109
while len(frontier) and fetched < self.max_pages:        # 93  ← bounded by maxVisits (see §6)
    item = frontier.pop()
    res = self.fetcher.get(item.url)                     # 98  ← SAME compliance-gated fetch, every time
    if not res.ok:
        status = "blocked" if "robots" in res.reason.lower() or "blocked" in res.reason.lower() else "error"  # 101
        ...
        result.visited.append({"url": item.url, "status": status, "note": res.reason})  # 104  ← audit trail
        continue                                         # 109  ← a blocked URL is dropped, never forced
```

A blocked or refused URL is recorded and skipped. It is **never** re-attempted
through some other channel.

### 5a. Where Scrapling *is* used: parsing (post-fetch, compliance-neutral)

Scrapling’s strength here is its **`Selector` parser** (`src/crawler/parsing.py`:
`selector()`:17, `extract_anchors()`:30, `text_and_contacts()`:57), used to read
the HTML the gated fetcher already returned — extracting links (`extract_anchors`,
used by `frontier.extract_links` at `src/crawler/frontier.py:79`) and visible text
+ contact counts (`text_and_contacts`, used by the page classifier
`src/crawler/classifier.py::_visible_text`), with BeautifulSoup as an automatic
fallback. This stage **touches no network** — it only parses bytes
already fetched under the gate — so it has **no compliance implications**; it’s
noted only for accuracy about where Scrapling lives in the pipeline.

---

## 6. The scope is bounded — no runaway crawling

Three independent caps keep the crawl small and proportionate:

- **maxVisits** — `discovery.py:93` stops the loop at `fetched < self.max_pages`.
- **max_depth** — links past the configured depth are not enqueued
  (`discovery.py:128` only extends the frontier while `item.depth < self.max_depth`).
- **maxExtracts** — only the top-scoring discovered pages are actually read for
  data (**`src/main.py:292`** slices `result.data_page_urls[:max_extracts]`).

We take a little, from a few pages, on one site — not a mass harvest.

---

## 7. We stay on the seed site — same-domain restriction

The crawler does not wander across the web from one site to another:

```python
# src/crawler/frontier.py:79-91  (extract_links)
def extract_links(html, base_url, *, same_domain=True):  # 79  ← default: stay on-site
    ...
    if same_domain and not same_site(base_url, absolute): # 91  ← drop off-site links
        continue

# src/crawler/discovery.py:64,72,128
same_domain: bool = True            # 64  default
self.same_domain = same_domain      # 72
links = extract_links(res.content, item.url, same_domain=self.same_domain)  # 128
```

So pointing it at one brokerage’s site keeps it on that site.

---

## 8. Everything is transparent and auditable

Legality also means being able to show what was done:

- **Every fetch outcome is recorded** — `discovery.py:104` (blocked/error) and
  `:121` (ok), each with its reason.
- **A manifest is written per crawl** to `runs/crawl_<timestamp>.json` (seed,
  prompt, intent, stats, every visited URL + outcome) — `src/main.py::crawl`.
- **Blocks are explained honestly** — `diagnose()` (`src/crawler/discovery.py:155-199`) turns the
  recorded outcomes into a plain-language reason shown in the UI history and
  results, e.g. *“N URL(s) were disallowed by robots.txt (the crawler obeys it and
  never bypasses)”* (`:176-178`) or *“the site returned 403 Forbidden — almost
  always anti-bot protection, which this tool does not bypass”* (`:179-181`).
- **The Flow tab** in the UI shows these same controls as a diagram, kept in sync
  with the code.

If a site comes back empty, the tool tells you it was *blocked* — it does not
quietly find a way around the block.

---

## 9. The policy is documented — not just coded

Compliance here is also written down and made visible at runtime:

- **The stated policy** — `README.md` (the “Compliance notice” blockquote near the
  top): the tool collects only **publicly available** information, honors
  `robots.txt` + polite rate limits, and **does not bypass** CAPTCHA / anti-bot /
  login / paywall, nor impersonate browsers. Sites that refuse an honest client are
  *logged with the reason, never bypassed.*

- **The central tooling decision** — `docs/TOOLING_DECISION.md`: Scrapling is used
  with **stealth / anti-bot OFF**. It states plainly that `StealthyFetcher` *can*
  bypass Cloudflare/Turnstile/fingerprinting (`:23-25`) and that using it **would
  violate the policy** — *“Scrapling can bypass, but bypassing is the one thing we
  won’t do”* (`:28`). The decision table (`:36-37`) chooses self-healing selectors
  and vanilla Playwright, never stealth.

- **Blocks are surfaced live, per fetch** — the tool is general (no static
  approved-source list); instead every crawl records each URL's outcome and
  `diagnose()` (§8) tells you exactly why a site yielded nothing — *“disallowed by
  robots.txt”*, *“403 Forbidden — anti-bot, not bypassed”*, etc. — shown in the UI
  history and results. Compliance is enforced and reported per request, up front.

---

## Summary — what makes it legal & ethical, and where it lives

| Safeguard | Where in code |
|---|---|
| **Build:** Scrapling `[fetchers]`/stealth extra disabled; no proxy/CAPTCHA/anti-bot packages | `requirements.txt:14` (+ `:1-27`) |
| **Build:** only a vanilla Chromium is installed (no anti-detect browser) | `Dockerfile:19-23` |
| **Build:** local-only LLM, no proxy service, no secrets needed | `docker-compose.yml:5-21,29,63` |
| UI entry — `/crawl` validates input, adds no header tricks | `src/web/app.py:234-252` → `src/web/runner.py` → `src/main.py:195` (`crawl()`) |
| Honest, self-identifying User-Agent | `config/rate_limits.yaml:5` |
| Fetch engine chosen (non-stealth) | `src/sources/base.py:151` (`make_fetcher`) |
| robots.txt obeyed before every fetch | `src/sources/base.py:69-72` → `src/utils/compliance.py:36-40` → `src/utils/robots.py:17-42` |
| Polite per-domain rate limiting (≥2 s) | `src/sources/base.py:73` → `src/utils/rate_limit.py:31-45` |
| Bounded, backed-off retries (no pounding) | `src/sources/base.py:75-77` → `src/utils/retry.py:10-39` |
| Fetch is plain `httpx` — Scrapling fetcher’s stealth/fingerprint deps (`curl_cffi`/`browserforge`) are not installed | `src/sources/base.py:87-148` (`_raw_get` fallback `:133-148`) |
| HTML parsing via Scrapling `Selector` (post-fetch, no network), bs4 fallback | `src/crawler/parsing.py:30,57` (used by `src/crawler/frontier.py:79`, `src/crawler/classifier.py::_visible_text`) |
| Gate applied to every discovered link | `src/crawler/discovery.py:98-109` |
| Bounded scope (maxVisits / depth / maxExtracts) | `src/crawler/discovery.py:93` · `:128` · `src/main.py:292` |
| Same-domain only (no web-wide wandering) | `src/crawler/frontier.py:79-91` + `src/crawler/discovery.py:64,72,128` |
| Full audit trail + honest “why blocked” | `src/crawler/discovery.py:104,121` · `diagnose()` `:155-199` (robots `:176-178`, 403 `:179-181`) + `runs/crawl_*.json` |
| **Doc:** stated compliance policy (no-bypass) | `README.md` (Compliance notice) |
| **Doc:** stealth-OFF tooling decision (Scrapling *can* bypass; we won't) | `docs/TOOLING_DECISION.md` (`:23-28`, `:36-37`) |

**What is deliberately NOT in the codebase:** CAPTCHA solvers, Cloudflare/
PerimeterX/Turnstile defeats, browser-fingerprint spoofing, residential/rotating
proxies, login or paywall circumvention, or any code that ignores `robots.txt`.
Their absence is itself a compliance control.
