"""DiscoveryCrawler: the FetchFox-style ``crawl`` loop.

A polite, same-domain best-first search seeded from one URL. Each pop:

1. fetch via the project's compliance-gated fetcher (robots + no-bypass + rate
   limit + retry are all inherited -- the crawler adds no new network policy);
2. classify the page -- if it lists the target entity, record it as a
   *discovered data page*;
3. extract same-site links, score them, and enqueue for later depth.

The loop stops at ``max_pages`` fetched or when the frontier drains. Every fetch
outcome (ok / blocked / error) is recorded so the run is auditable. The result
is the list of discovered data-page URLs, which the caller turns into a runtime
source config for the existing extraction pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..sources.base import HttpFetcher, make_fetcher
from ..utils.provenance import now_iso
from .classifier import LinkClassifier, PageClassifier
from .frontier import Frontier, extract_links
from .intent import CrawlIntent
from .llm import OllamaClient

log = logging.getLogger(__name__)


@dataclass
class DiscoveredPage:
    url: str
    score: float
    depth: int
    estimated_records: int
    reason: str


@dataclass
class CrawlResult:
    intent: CrawlIntent
    data_pages: list[DiscoveredPage] = field(default_factory=list)
    visited: list[dict] = field(default_factory=list)  # {url, status, note}
    stats: dict = field(default_factory=dict)

    @property
    def data_page_urls(self) -> list[str]:
        # Highest-confidence pages first.
        return [p.url for p in sorted(self.data_pages, key=lambda p: -p.score)]


class DiscoveryCrawler:
    def __init__(
        self,
        intent: CrawlIntent,
        client: OllamaClient | None = None,
        fetcher: HttpFetcher | None = None,
        *,
        max_pages: int = 40,
        max_depth: int = 3,
        same_domain: bool = True,
        on_event=None,
    ):
        self.intent = intent
        self.client = client
        self.fetcher = fetcher or make_fetcher()
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.same_domain = same_domain
        self.on_event = on_event  # optional callable(dict) for live progress
        self.links = LinkClassifier(client)
        self.pages = PageClassifier(client)

    def _emit(self, **event) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - progress must never break the crawl
                pass

    def run(self, seed_url: str) -> CrawlResult:
        frontier = Frontier()
        frontier.push(seed_url, score=1.0, depth=0)
        result = CrawlResult(intent=self.intent)
        fetched = 0
        blocked = 0
        errors = 0
        started = now_iso()

        while len(frontier) and fetched < self.max_pages:
            item = frontier.pop()
            if item is None:
                break
            t0 = time.monotonic()
            res = self.fetcher.get(item.url)
            secs = round(time.monotonic() - t0, 1)
            if not res.ok:
                status = "blocked" if "robots" in res.reason.lower() or "blocked" in res.reason.lower() else "error"
                blocked += status == "blocked"
                errors += status == "error"
                result.visited.append({"url": item.url, "status": status, "note": res.reason})
                log.info("crawl %-7s d%d %s (%s)", status, item.depth, item.url, res.reason[:80])
                self._emit(kind="visit", seconds=secs, url=item.url, status=status,
                           note=res.reason[:120], fetched=fetched,
                           data_pages=len(result.data_pages))
                continue
            fetched += 1

            verdict = self.pages.classify(item.url, res.content, self.intent)
            note = verdict.reason
            is_data = verdict.is_data_page
            if is_data:
                result.data_pages.append(DiscoveredPage(
                    url=item.url, score=max(item_score(item), 0.5), depth=item.depth,
                    estimated_records=verdict.estimated_records, reason=verdict.reason,
                ))
                note = f"DATA PAGE (~{verdict.estimated_records}): {verdict.reason}"
            result.visited.append({"url": item.url, "status": "ok", "note": note})
            log.info("crawl ok      d%d %s -> %s", item.depth, item.url, note[:90])
            self._emit(kind="visit", seconds=secs, url=item.url,
                       status="data_page" if is_data else "ok", note=note[:120],
                       fetched=fetched, data_pages=len(result.data_pages))

            if item.depth < self.max_depth:
                links = extract_links(res.content, item.url, same_domain=self.same_domain)
                for ln, score in self.links.score_links(item.url, links, self.intent):
                    if score >= 0:  # negatives are excluded (login/legal/etc.)
                        frontier.push(ln.url, score, item.depth + 1, ln.anchor)

        result.stats = {
            "seed": seed_url,
            "pages_fetched": fetched,
            "blocked": blocked,
            "errors": errors,
            "data_pages_found": len(result.data_pages),
            "llm_calls": self.client.calls if self.client else 0,
            "started": started,
            "finished": now_iso(),
            "engine": self.fetcher.engine,
            "model": self.client.model if self.client else None,
        }
        log.info("crawl done: fetched=%d data_pages=%d blocked=%d errors=%d llm_calls=%d",
                 fetched, len(result.data_pages), blocked, errors, result.stats["llm_calls"])
        return result


def item_score(item) -> float:
    """Frontier items carry their priority as a negated score."""
    return -getattr(item, "neg_score", -0.5)


def diagnose(result: CrawlResult) -> str:
    """Plain-language explanation of why a crawl yielded no data pages.

    Built from the per-URL fetch/classify outcomes so a blocked or empty crawl
    says exactly what stopped it (robots.txt, 403/anti-bot, timeouts, or
    'fetched but nothing looked like a listing').
    """
    s = result.stats
    fetched = s.get("pages_fetched", 0)
    blocked = s.get("blocked", 0)
    errors = s.get("errors", 0)
    found = s.get("data_pages_found", 0)
    if found:
        return f"Found {found} data page(s)."

    notes = [(v.get("status"), (v.get("note") or "")) for v in result.visited]
    robots = sum(1 for st, n in notes if st == "blocked")
    forbidden = sum(1 for st, n in notes if "403" in n or "forbidden" in n.lower())
    timeouts = sum(1 for st, n in notes
                   if st == "error" and ("timeout" in n.lower() or "timed out" in n.lower()))
    reasons: list[str] = []
    if robots:
        reasons.append(f"{robots} URL(s) were disallowed by the site's robots.txt "
                       "(the crawler obeys robots.txt and never bypasses it)")
    if forbidden:
        reasons.append("the site returned 403 Forbidden — almost always anti-bot "
                       "protection, which this tool does not bypass")
    if timeouts:
        reasons.append(f"{timeouts} request(s) timed out")
    other = errors - forbidden - timeouts
    if other > 0:
        ex = next((n for st, n in notes if st == "error"), "")
        reasons.append(f"{other} request(s) failed (e.g. “{ex[:90]}”)")
    if fetched and found == 0:
        ex = next((n for st, n in notes if st == "ok"), "")
        tail = f" (e.g. “{ex[:90]}”)" if ex else ""
        reasons.append(f"{fetched} page(s) were fetched OK, but none looked like a "
                       f"listing of {result.intent.entity}{tail}")
    if not reasons:
        if fetched == 0 and blocked == 0 and errors == 0:
            reasons.append("nothing was fetched — the seed URL may be wrong or unreachable")
        else:
            reasons.append("no pages matching your request were found")
    msg = "; ".join(reasons) + "."
    return msg[0].upper() + msg[1:]
