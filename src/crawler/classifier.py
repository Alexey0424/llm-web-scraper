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
from dataclasses import dataclass
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
            "You rank candidate links by how likely each leads to a page that "
            f"LISTS many '{intent.entity}' records with contact details. Output "
            'ONLY JSON: {"scores": [{"i": <index>, "p": <0..1>}, ...]} where p is '
            "the probability the link leads toward such a listing/roster (or a "
            "pagination page of one). High p for directory/roster/agents/team "
            "pages; low p for legal, careers, single property listings."
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


# ---------------------------------------------------------------------------
# Page classification (is THIS page a data/roster page worth extracting?)
# ---------------------------------------------------------------------------
@dataclass
class PageVerdict:
    is_data_page: bool
    estimated_records: int
    reason: str


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

        system = (
            f"Decide whether the page text below is a LISTING/ROSTER of multiple "
            f"'{intent.entity}' records (names + contact info), not a single "
            "profile, a blog post, or a search box. Output ONLY JSON: "
            '{"is_listing": true|false, "count": <approx number of records>, '
            '"reason": "<short>"}'
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
            return PageVerdict(
                is_data_page=is_list or heuristic_hit,
                estimated_records=max(count, contact_signal),
                reason=str(data.get("reason", "")).strip()[:160] or "llm",
            )
        return PageVerdict(heuristic_hit, contact_signal,
                           f"heuristic fallback: {contact_signal} contact links")
