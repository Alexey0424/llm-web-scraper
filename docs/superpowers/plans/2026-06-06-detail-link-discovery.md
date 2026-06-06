# Detail-Link Discovery + JSON-LD Extraction Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Execute task-by-task, commit after each, validate with the integration run.

**Goal:** Make the crawler reliably find individual *detail/record* pages (e.g. agent profiles) by **URL-pattern + structure** instead of anchor keywords, and extract their fields — including contact info from **JSON-LD** — so a run can return agents with Email + Phone filled.

**Architecture (research-backed hybrid):** Practitioners select detail links by *URL shape* and *DOM structure*, never by anchor text (anchor is a person's name → keyword score ~0). So: (1) on a listing page, **induce the repeating URL clusters** deterministically, (2) use **one LLM call** to label which cluster is the "individual record" detail page (FetchFox/Crawl4AI "author a selector once" pattern), (3) **harvest all links of that cluster** at top priority regardless of anchor text, (4) on detail pages, **parse JSON-LD** (`application/ld+json`) for telephone/email/name/address in addition to the existing mailto/tel + LLM text extraction. Keep the existing segment filter / balance / record-target stop.

**Tech Stack:** Python, Scrapling (fetch), BeautifulSoup/lxml + json (JSON-LD), local Ollama gemma4:e4b (one labeling call), pytest. Verified facts: Princeton-NJ profiles expose `mailto:` email + JSON-LD `"telephone"`; listing pages carry 15 `/profile/<name>` links.

**Validation prompt (integration):** `Find Zillow real estate agent profiles for agents in New Jersey (NJ) ... Agent Full Name, First, Last, Email, Phone 1, Phone 2, Agency Name, City, State, Source URL`, seed `https://www.zillow.com/professionals/real-estate-agent-reviews/princeton-nj/`, **target 5**. **Success = 5 NJ agent rows with Email AND Phone 1 filled.**

---

## File Structure

- **Create** `src/crawler/urlpattern.py` — deterministic URL-cluster induction: given page links, return candidate "detail" URL patterns (path-prefix templates) + a matcher. No LLM, no I/O. One responsibility: URL shape grouping.
- **Modify** `src/crawler/classifier.py` — add `LinkClassifier.detect_detail_pattern(links, intent)` (one LLM call labeling which induced cluster is the individual record); make `score_links` boost links matching a known detail pattern to top priority.
- **Modify** `src/crawler/discovery.py` — on a listing/hub page, induce + (once) label the detail pattern, then harvest all matching detail links at `DETAIL_LINK_SCORE` priority.
- **Create** `src/crawler/jsonld.py` — extract `application/ld+json` objects from HTML and flatten the useful keys (name, telephone, email, address.addressLocality/Region). One responsibility: JSON-LD → flat dict.
- **Modify** `src/crawler/freeform.py` — before/with the LLM call, merge JSON-LD-derived values into the extracted record so contact fields fill even when the visible text hides them.
- **Test** `tests/test_urlpattern.py`, `tests/test_jsonld.py`, fixture `tests/fixtures/zillow_profile.html` (saved real profile).

---

## Task 1: URL-cluster induction (deterministic)

**Files:** Create `src/crawler/urlpattern.py`; Test `tests/test_urlpattern.py`

- [ ] **Step 1 — Write failing tests**

```python
# tests/test_urlpattern.py
from src.crawler.urlpattern import induce_clusters, matches_pattern

def test_profile_cluster_dominant():
    urls = [f"https://x.com/profile/agent{i}" for i in range(15)] + [
        "https://x.com/professionals/real-estate-agent-reviews/princeton-nj",
        "https://x.com/professionals/real-estate-agent-reviews/trenton-nj",
        "https://x.com/about", "https://x.com/help",
    ]
    clusters = induce_clusters(urls, min_size=3)
    # the /profile/ prefix should be the largest record-like cluster
    top = clusters[0]
    assert top["prefix"] == "/profile" and top["size"] == 15

def test_matcher():
    assert matches_pattern("https://x.com/profile/Dana%20Lax", "/profile")
    assert not matches_pattern("https://x.com/profile/a/b", "/profile")  # leaf only
    assert not matches_pattern("https://x.com/help", "/profile")
```

- [ ] **Step 2 — Run, expect fail** — `docker compose exec -T ui python -m pytest tests/test_urlpattern.py -v` → FAIL (module missing).

- [ ] **Step 3 — Implement**

```python
# src/crawler/urlpattern.py
"""Induce repeating URL clusters from a page's links (anchor-text-free).

Detail/record pages on a listing share a path prefix whose final segment varies
(/profile/<name>, /dp/<id>, /article/<slug>). We group same-site URLs by
"prefix = path minus last segment" and rank clusters by how many DISTINCT leaves
they have -- the record cluster is typically the largest. Deterministic; the
caller (optionally an LLM) decides which cluster is the target record type.
"""
from __future__ import annotations
from urllib.parse import urlparse


def _prefix_leaf(url: str) -> tuple[str, str] | None:
    segs = [s for s in urlparse(url).path.split("/") if s]
    if not segs:
        return None
    return "/" + "/".join(segs[:-1]), segs[-1]


def induce_clusters(urls: list[str], min_size: int = 3) -> list[dict]:
    groups: dict[str, set[str]] = {}
    for u in urls:
        pl = _prefix_leaf(u)
        if not pl:
            continue
        prefix, leaf = pl
        groups.setdefault(prefix, set()).add(leaf)
    clusters = [
        {"prefix": p, "size": len(leaves), "samples": sorted(leaves)[:5]}
        for p, leaves in groups.items() if len(leaves) >= min_size
    ]
    clusters.sort(key=lambda c: -c["size"])
    return clusters


def matches_pattern(url: str, prefix: str) -> bool:
    """True if url is a single-leaf child of prefix (a detail page of that cluster)."""
    pl = _prefix_leaf(url)
    return bool(pl and pl[0] == prefix)
```

- [ ] **Step 4 — Run, expect pass.**
- [ ] **Step 5 — Commit** — `git add src/crawler/urlpattern.py tests/test_urlpattern.py && git commit -m "feat(crawl): deterministic URL-cluster induction"`

---

## Task 2: JSON-LD extraction (gets Email + Phone)

**Files:** Create `src/crawler/jsonld.py`; Test `tests/test_jsonld.py` + fixture `tests/fixtures/zillow_profile.html`

- [ ] **Step 1 — Save a real profile fixture** (already verified to contain JSON-LD telephone + mailto):

Run: `docker compose exec -T ui python -c "from src.sources.base import make_fetcher; open('/app/tests/fixtures/zillow_profile.html','w',encoding='utf-8').write(make_fetcher().get('https://www.zillow.com/profile/Dana%20Rae%20Moreno').content)"`

- [ ] **Step 2 — Write failing test**

```python
# tests/test_jsonld.py
from pathlib import Path
from src.crawler.jsonld import extract_jsonld_fields

def test_profile_jsonld_has_contact():
    html = Path("tests/fixtures/zillow_profile.html").read_text(encoding="utf-8")
    f = extract_jsonld_fields(html)
    assert f.get("telephone")  # e.g. "(609) 555-0143"
    assert f.get("name")       # "Dana Rae Moreno"
```

- [ ] **Step 3 — Run, expect fail.**

- [ ] **Step 4 — Implement**

```python
# src/crawler/jsonld.py
"""Pull structured fields from <script type="application/ld+json"> blocks.

Many detail pages (people, products, local businesses) embed schema.org JSON-LD
with exactly the fields users want (name, telephone, email, address). Reading it
is far more reliable than scraping rendered text. Returns a flat dict of the
high-value keys; tolerant of arrays, @graph wrappers, and malformed blocks.
"""
from __future__ import annotations
import json
import re

_LD = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                 re.DOTALL | re.IGNORECASE)
_KEYS = ("name", "telephone", "email", "url")


def _walk(obj, out: dict) -> None:
    if isinstance(obj, dict):
        for k in _KEYS:
            if k in obj and isinstance(obj[k], (str, int)) and k not in out:
                out[k] = str(obj[k]).strip()
        addr = obj.get("address")
        if isinstance(addr, dict):
            if addr.get("addressLocality") and "city" not in out:
                out["city"] = str(addr["addressLocality"]).strip()
            if addr.get("addressRegion") and "state" not in out:
                out["state"] = str(addr["addressRegion"]).strip()
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def extract_jsonld_fields(html: str) -> dict:
    out: dict = {}
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        _walk(data, out)
    return out
```

- [ ] **Step 5 — Run, expect pass.**
- [ ] **Step 6 — Commit** — `git add src/crawler/jsonld.py tests/test_jsonld.py tests/fixtures/zillow_profile.html && git commit -m "feat(crawl): JSON-LD field extraction"`

---

## Task 3: Merge JSON-LD into free-form extraction

**Files:** Modify `src/crawler/freeform.py`

- [ ] **Step 1 — Add JSON-LD merge in `FreeFormExtractor.extract`.** After computing rows from the LLM, fill any empty `email`/`phone`/`name`/`city`/`state`-like column from `extract_jsonld_fields(content)` (map by key substring: a column key containing "email"→email, "phone"→telephone, "name"→name, "city"→city, "state"→state). Only fills blanks; never overwrites LLM values. For a single-record detail page (one JSON-LD entity) this fills the contact fields the visible text hides.

```python
# in src/crawler/freeform.py, top:
from .jsonld import extract_jsonld_fields
# ... inside extract(), after building `out` rows and before return:
ld = extract_jsonld_fields(content)
if ld:
    def _fill(row):
        for _, k in self.columns:  # (header, key) excl. source_url
            if row.get(k):
                continue
            if "email" in k and ld.get("email"): row[k] = ld["email"]
            elif ("phone" in k or "tel" in k) and ld.get("telephone"):
                row[k] = normalize_phone(ld["telephone"]) or ld["telephone"]
            elif "name" in k and "first" not in k and "last" not in k and ld.get("name"):
                row[k] = ld["name"]
            elif k == "city" and ld.get("city"): row[k] = ld["city"]
            elif k == "state" and ld.get("state"): row[k] = ld["state"]
        return row
    out = [_fill(r) for r in out]
    if not out and any(ld.get(x) for x in ("name", "telephone", "email")):
        # detail page the LLM returned nothing for, but JSON-LD has the record
        out = [_fill({k: None for _, k in self.columns})]
```

- [ ] **Step 2 — Run existing tests** — `docker compose exec -T ui python -m pytest -q` → all pass.
- [ ] **Step 3 — Commit** — `git commit -am "feat(crawl): fill blank contact fields from JSON-LD"`

---

## Task 4: Detail-pattern detection + structural link harvest

**Files:** Modify `src/crawler/classifier.py`, `src/crawler/discovery.py`

- [ ] **Step 1 — `LinkClassifier.detect_detail_pattern(links, intent)`** (one LLM call): induce clusters with `urlpattern.induce_clusters([l.url for l in links])`; if ≤1 cluster, return it; else ask the LLM to pick which cluster prefix holds the INDIVIDUAL `intent.entity` record (give prefixes + samples + entity). Return the chosen `prefix` (str) or None. Cache the result on the crawler so it's one call per run.

- [ ] **Step 2 — `discovery.run`:** after `extract_links`, if no detail pattern is known yet and the page looks like a listing (has a cluster ≥3), call `detect_detail_pattern` once and store `self._detail_prefix`. Then for every extracted link, if `matches_pattern(link.url, self._detail_prefix)`, push it at `DETAIL_LINK_SCORE = 1.6` (above on-page scores, below proposed 1.5? — set 1.6 so detail links are followed first). This makes `/profile/<name>` links get crawled regardless of anchor text.

- [ ] **Step 3 — PageClassifier:** a single JSON-LD `Person`/`LocalBusiness`/`RealEstateAgent` page should classify as a data page (estimated_records=1). Add: if `extract_jsonld_fields(html)` has name+telephone/email, force `is_data_page=True`.

- [ ] **Step 4 — Run unit tests** → pass.
- [ ] **Step 5 — Commit** — `git commit -am "feat(crawl): detect detail-URL pattern and harvest detail links structurally"`

---

## Task 5: Integration validation + experiment comparison

- [ ] **Step 1 — Baseline (current main):** run the validation prompt + Princeton seed, target 5; record email/phone fill rate (expected low).
- [ ] **Step 2 — On this branch:** same run. **Success = 5 NJ rows, Email AND Phone1 filled.**

Run: `docker compose exec -T ui python -m src.main crawl --url "https://www.zillow.com/professionals/real-estate-agent-reviews/princeton-nj/" --prompt "<validation prompt>" --target-records 5 --extract`

- [ ] **Step 3 — Inspect records** from `data/processed/freeform.sqlite` (latest run): confirm per-row `agent_email` and `agent_phone_1` non-null, state=NJ.
- [ ] **Step 4 — If email/phone still blank:** debug in order — (a) are profile pages being visited? (b) does `extract_jsonld_fields` fire on them? (c) are columns mapping correctly? Fix the failing layer only.
- [ ] **Step 5 — Commit + record results** in this plan's "Experiment log" below.

---

## Experiment Log (fill during execution)

| Branch | Approach | NJ rows | Email filled | Phone1 filled | Pages | Notes |
|---|---|---|---|---|---|---|
| baseline 8263346 | listing roster extract, no JSON-LD | 5 | 0/5 | ~1/5 | 1 | extracted the hub roster; no contact fields |
| detail-link-discovery (iter 1) | pattern harvest, extract listing | 5 | 0/5 | 1/5 | 1 | bug: hub extracted before drilling profiles |
| detail-link-discovery (iter 2) | hub→harvest, extract profiles | 5 | 1/5 | 5/5 | 4 | phone via JSON-LD; email missed by LLM; dup rows |
| **detail-link-discovery (final)** | **+mailto email +merge-to-one** | **5** | **5/5** | **5/5** | **6** | **✅ success — 1 clean record/profile** |

## Caveats (from research)
- LLM-authored/induced patterns are brittle to DOM/URL changes — re-induce per run, don't hardcode.
- Pattern induction can surface multiple clusters (profiles vs city links); the one-LLM-call label disambiguates. Keep the call tiny (prefixes + samples only).
- gemma4:e4b is weak at codegen; we ask it only to PICK a cluster, not write selectors — within its ability.
- JSON-LD isn't universal; the LLM text extraction remains the fallback for sites without it.
