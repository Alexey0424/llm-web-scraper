# Tooling Decision

> **Note (2026-06-04):** the original fixed-schema scraping pipeline was removed;
> only the LLM discovery crawler remains. The decision below — **Scrapling with
> stealth/anti-bot OFF, no bypass** — still governs the crawler (Scrapling is used
> as the HTML parser; fetching is plain `httpx`). The pipeline specifics mentioned
> further down are historical.

## Summary

**Scrapling-first for parsing/adaptive selectors, with its stealth/anti-bot
features deliberately DISABLED.** Static pages are fetched with `httpx` and
parsed with `BeautifulSoup`; JS-only pages use vanilla Playwright. We never use
any tool to bypass CAPTCHA, Cloudflare/Turnstile, fingerprinting, login, or
paywalls.

## The Scrapling evaluation

Scrapling (https://github.com/D4Vinci/Scrapling) was the user's preferred
default. We evaluated it honestly.

**What Scrapling is genuinely good at, and which we use:**
- *Adaptive / self-healing selectors* (`auto_match`) — re-locates elements when a
  site's markup shifts. Valuable for long-lived roster scrapers. Used via
  `extractors/html_extractors.py::adaptive_extract`.
- Clean, fast parsing API.

**What Scrapling markets that we explicitly DO NOT use:**
- `StealthyFetcher` claims to *"bypass all types of Cloudflare's
  Turnstile/Interstitial"*, spoof browser fingerprints, and impersonate TLS.
  This is anti-bot **bypass**. Using it would violate the project's compliance
  rules and target sites' Terms of Service. **Disabled. Not invoked anywhere.**

This is the central decision: Scrapling *can* bypass, but bypassing is the one
thing this project refuses to do. We keep the resilience, drop the evasion.

## Per-source-type choice

| Source type                     | Tool                              | Why |
|---------------------------------|-----------------------------------|-----|
| Static brokerage roster page    | httpx + BeautifulSoup             | Fast, no browser, fully sufficient |
| Roster with shifting HTML       | Scrapling Adaptor (stealth OFF)   | Self-healing selectors survive markup churn |
| JS-rendered roster (permitted)  | Playwright (vanilla, no stealth)  | Needs a real renderer; only where ToS/robots allow |
| Embedded JSON-LD / Next.js data | json_extractors                   | Structured, high-precision |
| CAPTCHA/anti-bot protected site | **skip + log**                    | Bypass is prohibited by policy |

## Why not Scrapy?

Scrapy is excellent for large homogeneous crawls, but our target is many small,
heterogeneous roster pages with bespoke selectors. A lightweight plug-in source
interface + httpx is simpler, easier to test, and adds less operational weight.
Scrapy adds value only if/when we crawl a single very large permitted directory;
it can be added behind the same `Source` interface later without disruption.

## Outcome

A **Scrapling-first, compliance-bounded** system: resilient parsing where it
helps, plain httpx where a browser is unnecessary, vanilla Playwright only when a
page truly needs rendering — and **no circumvention anywhere**.
