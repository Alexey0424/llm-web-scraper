"""Link and page classifiers — the crawl's many short ``runCompletion`` calls.

Both classifiers are **heuristic-first, LLM-refined**:

* The heuristic uses the intent's include/exclude keywords on the URL + anchor
  text. It is deterministic, free, and works with no LLM (so tests and offline
  runs behave). It also bounds cost by pre-filtering links before any LLM call.
* When an LLM is available, it re-ranks the top heuristic candidates (links) or
  judges a page's visible text (page). The LLM's signal is blended with the
  heuristic, never trusted blindly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .frontier import Link
from .intent import CrawlIntent
from .llm import OllamaClient
from .parsing import text_and_contacts

log = logging.getLogger(__name__)

_MAILTO = re.compile(r"mailto:", re.IGNORECASE)
_TEL = re.compile(r"\btel:", re.IGNORECASE)


def _kw_score(text: str, include: list[str], exclude: list[str]) -> float:
    t = text.lower()
    if any(x and x in t for x in exclude):
        return -1.0
    hits = sum(1 for x in include if x and x in t)
    return min(1.0, 0.2 * hits) if hits else 0.0


# ---------------------------------------------------------------------------
# Link classification (which links to enqueue, and at what priority)
# ---------------------------------------------------------------------------
class LinkClassifier:
    def __init__(self, client: OllamaClient | None = None, llm_top_k: int = 25):
        self.client = client
        self.llm_top_k = llm_top_k

    def score_links(self, page_url: str, links: list[Link],
                    intent: CrawlIntent) -> list[tuple[Link, float]]:
        scored: list[tuple[Link, float]] = []
        for ln in links:
            base = _kw_score(ln.url + " " + ln.anchor,
                             intent.include_keywords, intent.exclude_keywords)
            # Pagination URLs (?page=, /page/2) are valuable for rosters.
            if re.search(r"(?:[?&]page=\d+|/page/\d+|/p/\d+)", ln.url):
                base = max(base, 0.6)
            scored.append((ln, base))
        scored.sort(key=lambda x: x[1], reverse=True)

        if self.client is None or not scored:
            return scored
        candidates = [ln for ln, s in scored[: self.llm_top_k] if s >= 0]
        if not candidates:
            return scored
        boosts = self._llm_rank(page_url, candidates, intent)
        if boosts:
            scored = [
                (ln, min(1.0, s + boosts.get(ln.url, 0.0))) for ln, s in scored
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _llm_rank(self, page_url: str, candidates: list[Link],
                  intent: CrawlIntent) -> dict[str, float]:
        listing = "\n".join(
            f"{i}. {ln.anchor or '(no text)'} -> {ln.url}"
            for i, ln in enumerate(candidates)
        )
        system = (
            f"You rank candidate links for a crawler collecting '{intent.entity}' "
            "records. Give HIGH p to TWO kinds of links: (1) listing/roster/"
            "directory pages that index many records (and their pagination), and "
            "(2) an INDIVIDUAL record's detail/profile page (e.g. a single agent's "
            "profile), because that is where per-record fields like phone, email, "
            "office and address usually live. Give LOW p to legal, careers, login, "
            "blog, and property/home-for-sale listings. Output ONLY JSON: "
            '{"scores": [{"i": <index>, "p": <0..1>}, ...]} where p is the '
            "probability the link leads toward target records."
        )
        user = f"Current page: {page_url}\nTarget: {intent.description}\n\nLINKS:\n{listing}"
        data = self.client.chat_json(system, user, default=None)
        out: dict[str, float] = {}
        if isinstance(data, dict) and isinstance(data.get("scores"), list):
            for row in data["scores"]:
                try:
                    idx = int(row["i"])
                    p = float(row["p"])
                except (KeyError, ValueError, TypeError):
                    continue
                if 0 <= idx < len(candidates):
                    out[candidates[idx].url] = max(0.0, min(1.0, p)) * 0.6
        return out

    def propose_urls(self, page_url: str, sample_urls: list[str],
                     intent: CrawlIntent, max_per_location: int = 3) -> list[str]:
        """FetchFox-style "query mode": invent same-domain URLs to visit.

        Given example URLs seen on the page plus the target entity/locations, ask
        the LLM to infer the site's URL pattern and emit OTHER same-domain URLs
        (e.g. other cities/locations) that aren't linked here. To raise the odds
        every target location is reached, it asks for SEVERAL major localities per
        location. Same-domain-filtered and bounded; whatever is enqueued still
        passes the normal compliance gate and page classifier, so wrong guesses
        (404s, off-target) are dropped.
        """
        if self.client is None:
            return []
        domain = urlparse(page_url).netloc.lower()
        samples = "\n".join(f"- {u}" for u in sample_urls[:20])
        locations = ", ".join(intent.states) if intent.states else "(any location)"
        n = max_per_location
        system = (
            f"You help a crawler find pages of '{intent.entity}' records on the "
            f"site '{domain}'. From the EXAMPLE URLs, infer the site's URL "
            "pattern, then output OTHER same-domain URLs (NOT already in the "
            "examples) that most likely list or detail the target records for the "
            "requested locations.\n"
            f"IMPORTANT: produce {n} URLs for EVERY requested location, one for "
            f"each of that location's {n} largest cities, so at least one exists. "
            "Do not stop at one city per location.\n"
            "Example: if the pattern is '.../reviews/<city>-<st>' and locations "
            "are [CA, TX], output: .../reviews/los-angeles-ca, "
            ".../reviews/san-diego-ca, .../reviews/san-jose-ca, "
            ".../reviews/houston-tx, .../reviews/dallas-tx, .../reviews/austin-tx.\n"
            'Output ONLY JSON: {"urls": ["https://...", ...]}. Every URL must be '
            f"on the exact domain '{domain}'. No prose."
        )
        user = (
            f"Target entity: {intent.entity}\n"
            f"Target locations: {locations}\n"
            f"Full request: {intent.description}\n\n"
            f"EXAMPLE URLs on the page:\n{samples}"
        )
        # Slightly higher temperature: this is a "brainstorm several candidate
        # localities" call, not a deterministic judgement -- greedy decoding tends
        # to emit only the single most-likely city per location.
        data = self.client.chat_json(system, user, temperature=0.5, default=None)
        out: list[str] = []
        seen = {u.rstrip("/") for u in sample_urls}
        if isinstance(data, dict) and isinstance(data.get("urls"), list):
            for u in data["urls"]:
                u = str(u).strip()
                if (u.startswith(("http://", "https://"))
                        and urlparse(u).netloc.lower() == domain
                        and u.rstrip("/") not in seen):
                    seen.add(u.rstrip("/"))
                    out.append(u)
        cap = n * max(1, len(intent.states or [1]))
        return out[:cap]


# ---------------------------------------------------------------------------
# Page classification (is THIS page a data/roster page worth extracting?)
# ---------------------------------------------------------------------------
@dataclass
class PageVerdict:
    is_data_page: bool
    estimated_records: int
    reason: str
    # Which of the requested fields actually appear on the page (#2 completeness).
    fields_found: list[str] = field(default_factory=list)
    # True if the page is mainly a hub/index that links to record pages (#5).
    links_to_records: bool = False


def _visible_text(html: str, limit: int = 6000) -> tuple[str, int, int]:
    # Scrapling Selector primary (get_all_text + CSS contact counts); bs4 fallback.
    return text_and_contacts(html, limit)


class PageClassifier:
    def __init__(self, client: OllamaClient | None = None):
        self.client = client

    def classify(self, url: str, html: str, intent: CrawlIntent) -> PageVerdict:
        text, mailtos, tels = _visible_text(html)
        # Heuristic: several contact links strongly suggest a roster page.
        contact_signal = mailtos + tels
        heuristic_hit = contact_signal >= 3

        if self.client is None:
            return PageVerdict(
                is_data_page=heuristic_hit,
                estimated_records=contact_signal,
                reason=f"heuristic: {mailtos} mailto + {tels} tel links",
            )

        fields = ", ".join(intent.fields) if intent.fields else "the requested fields"
        system = (
            f"You analyze a web page for a crawler collecting '{intent.entity}' "
            "records. Return ONLY JSON with these keys:\n"
            '  "is_listing": true if the page is EITHER a listing/roster of '
            f"multiple '{intent.entity}' records OR an individual record's detail/"
            "profile page with its own fields; false for blog/search/legal/"
            "marketing/pure-navigation pages.\n"
            '  "count": approximate number of records on the page (1 for a single '
            "profile).\n"
            '  "links_to_records": true if the page is mainly a hub/index that '
            "LINKS to many record pages rather than containing the records itself.\n"
            '  "fields_present": array naming which of these requested fields '
            f"actually appear in the page text: [{fields}]. Use the exact field "
            "names; [] if none.\n"
            '  "reason": short explanation.'
        )
        user = (
            f"URL: {url}\nTarget: {intent.description}\n"
            f"(page has {mailtos} mailto + {tels} tel links)\n\nTEXT:\n{text}"
        )
        data = self.client.chat_json(system, user, default=None)
        if isinstance(data, dict) and "is_listing" in data:
            is_list = bool(data.get("is_listing"))
            try:
                count = int(data.get("count") or contact_signal)
            except (ValueError, TypeError):
                count = contact_signal
            fp = data.get("fields_present")
            fields_found = ([str(x).strip() for x in fp if str(x).strip()]
                            if isinstance(fp, list) else [])
            return PageVerdict(
                is_data_page=is_list or heuristic_hit,
                estimated_records=max(count, contact_signal),
                reason=str(data.get("reason", "")).strip()[:160] or "llm",
                fields_found=fields_found,
                links_to_records=bool(data.get("links_to_records")),
            )
        return PageVerdict(heuristic_hit, contact_signal,
                           f"heuristic fallback: {contact_signal} contact links")
