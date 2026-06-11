"""DiscoveryCrawler: the FetchFox-style ``crawl`` loop.

A polite, same-domain best-first search seeded from one URL. Each pop:

1. fetch via the project's shared fetcher (Scrapling browser fetch; per-domain
   rate limit + bounded retry + block-page detection are all inherited -- the
   crawler adds no new network policy; robots gate currently off, Direct Mode);
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
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ..sources.base import HttpFetcher, make_fetcher
from ..utils.provenance import now_iso
from .classifier import LinkClassifier, PageClassifier, _kw_score
from .frontier import Frontier, Link, extract_links, normalize_url, same_site
from .parsing import extract_rel_next
from .intent import CrawlIntent
from .llm import OllamaClient
from .sitemap import fetch_sitemap_urls
from .urlpattern import matches_pattern

log = logging.getLogger(__name__)

# Priority for LLM-proposed target-location URLs. Deliberately above the 0..1
# range that on-page link scores occupy, so the user's explicit location intent
# is crawled (and extracted) before whatever the seed page happens to link to.
PROPOSED_URL_SCORE = 1.5

# Priority for links matching the detected individual-record URL pattern (e.g.
# /profile/<name>). Set highest so detail pages are crawled and extracted before
# anything else -- they are what the user ultimately wants, and they're found by
# URL shape, not anchor keywords (a name anchor would otherwise score ~0).
DETAIL_LINK_SCORE = 1.7

# Priority for the "next page" of a CONFIRMED data/hub page (a multi-page
# roster). Just under DETAIL_LINK_SCORE: keep extracting known-good rosters
# before exploring, but individual record pages still come first.
NEXT_PAGE_SCORE = 1.6

# Anchor texts that mean "the next page of this listing".
_NEXT_TEXT_RE = re.compile(
    r"^\s*(next(\s+page)?|more|older(\s+posts)?|siguiente|›|»|>|→)\s*[›»>→]*\s*$",
    re.IGNORECASE,
)


def pagination_links(html: str, base_url: str, links: list[Link]) -> list[str]:
    """Next-page URLs of this page: ``rel=next`` + "Next ›"-style anchors.

    URL-shape-agnostic on purpose — real pagination often uses opaque queries
    (?p=2) that no keyword or pattern heuristic would score. The caller decides
    whether the page deserves following (only data/hub pages do).
    """
    out: list[str] = []
    seen: set[str] = set()
    for href in extract_rel_next(html):
        u = normalize_url(urljoin(base_url, href))
        if (u.startswith(("http://", "https://")) and same_site(base_url, u)
                and u not in seen):
            seen.add(u)
            out.append(u)
    for ln in links:
        if _NEXT_TEXT_RE.match(ln.anchor or "") and ln.url not in seen:
            seen.add(ln.url)
            out.append(ln.url)
    return out


@dataclass
class DiscoveredPage:
    url: str
    score: float
    depth: int
    estimated_records: int
    reason: str
    fields_found: list[str] = field(default_factory=list)


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
        on_data_page=None,
        needed_segments=None,
        should_stop=None,
        use_sitemap: bool = True,
    ):
        self.intent = intent
        self.client = client
        self.fetcher = fetcher or make_fetcher()
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.same_domain = same_domain
        self.on_event = on_event  # optional callable(dict) for live progress
        # Optional callable(url, html, verdict) -> bool, invoked when a data page
        # is found. Returning False stops the crawl (used for record-target mode:
        # extract inline, reusing the fetched HTML, and stop once the target is
        # met). None = classic two-phase (discover fully, extract afterwards).
        self.on_data_page = on_data_page
        # Optional callable() -> set[str] of segments still needing coverage. When
        # set, the crawl skips URLs belonging to an already-full segment, so the
        # budget reaches UNDER-covered segments instead of re-drilling a
        # productive-but-full one (stops the yield-feedback from monopolizing).
        self.needed_segments = needed_segments
        # Optional callable() -> bool: when it returns True the crawl stops at the
        # next loop iteration (the web "Eject" button). Records already extracted
        # are kept — they're stored incrementally.
        self.should_stop = should_stop
        # Seed the frontier from /sitemap.xml (it often enumerates every record
        # page directly); off only for tests that need a links-only crawl.
        self.use_sitemap = use_sitemap
        self.links = LinkClassifier(client)
        self.pages = PageClassifier(client)
        # The detected individual-record URL prefix (e.g. "/profile"), learned
        # from the first page with a usable link cluster and then used to
        # harvest detail links. It must PROVE itself: if the first fetched URL
        # matching it does not classify as a data page, it is unlearned and
        # blacklisted so a later page can teach the real pattern (a homepage's
        # biggest cluster is often categories/cities, not records).
        self._detail_prefix = None
        self._detail_confirmed = False
        self._rejected_prefixes: set[str] = set()
        self._demoted: set[str] = set()

    def _emit(self, **event) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - progress must never break the crawl
                pass

    def _seed_from_sitemap(self, seed_url: str, frontier: Frontier,
                           result: CrawlResult) -> None:
        """Queue sitemap URLs: detail-pattern matches at top priority, other
        keyword-relevant URLs at their heuristic score. The pattern induced
        here is unconfirmed and goes through the same first-fetch validation
        as one learned from page links."""
        try:
            urls = fetch_sitemap_urls(seed_url, self.fetcher)
        except Exception as exc:  # noqa: BLE001 - sitemap is best-effort, never fatal
            log.warning("sitemap seeding failed for %s: %s", seed_url, exc)
            return
        if not urls:
            return
        if self._detail_prefix is None:
            cand = self.links.detect_detail_pattern(
                [Link(u, "") for u in urls], self.intent)
            if cand and cand not in self._rejected_prefixes:
                self._detail_prefix = cand
                log.info("crawl detail-pattern from sitemap: %s", self._detail_prefix)
        pushed = 0
        for u in urls:
            if self._detail_prefix and matches_pattern(u, self._detail_prefix):
                frontier.push(u, DETAIL_LINK_SCORE, 1, "(sitemap)")
                pushed += 1
            else:
                s = _kw_score(u, self.intent.include_keywords,
                              self.intent.exclude_keywords)
                if s > 0:
                    frontier.push(u, s, 1, "(sitemap)")
                    pushed += 1
        p = urlparse(seed_url)
        sm_url = f"{p.scheme}://{p.netloc}/sitemap.xml"
        note = f"sitemap: queued {pushed} of {len(urls)} URL(s)"
        result.visited.append({"url": sm_url, "status": "ok", "note": note})
        log.info("crawl %s", note)
        self._emit(kind="visit", url=sm_url, status="ok", note=note,
                   fetched=0, data_pages=0)

    def run(self, seed_url: str) -> CrawlResult:
        frontier = Frontier()
        frontier.push(seed_url, score=1.0, depth=0)
        result = CrawlResult(intent=self.intent)
        if self.use_sitemap:
            self._seed_from_sitemap(seed_url, frontier, result)
        fetched = 0
        blocked = 0
        errors = 0
        started = now_iso()
        # Yield-feedback (#1): records observed / pages seen, keyed by URL shape.
        shape_records: dict[str, int] = {}
        shape_seen: dict[str, int] = {}
        reproposed = False  # pattern re-proposal (#4) fires at most once
        steer_segs = self.intent.segments or self.intent.states

        ejected = False
        while len(frontier) and fetched < self.max_pages:
            if self.should_stop is not None and self.should_stop():
                ejected = True
                log.info("crawl ejected by user after %d page(s)", fetched)
                break
            item = frontier.pop()
            if item is None:
                break
            # A URL queued at top priority under a since-rejected detail prefix
            # would still drain the budget first; demote it once instead.
            if (self._rejected_prefixes and item.url not in self._demoted
                    and item_score(item) >= DETAIL_LINK_SCORE
                    and any(matches_pattern(item.url, p)
                            for p in self._rejected_prefixes)):
                self._demoted.add(item.url)
                frontier.visited.discard(item.url)
                frontier.push(item.url, 0.1, item.depth, item.anchor)
                continue
            # Balance steering: skip a URL whose segment is already full, so the
            # budget reaches under-covered segments (a skip is free -- no fetch).
            if steer_segs and self.needed_segments is not None:
                seg_of = url_segment(item.url, steer_segs)
                if seg_of is not None and seg_of not in self.needed_segments():
                    log.debug("crawl skip (segment %s full): %s", seg_of, item.url)
                    continue
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
            is_data = verdict.is_data_page

            # Extract links + detect the record URL pattern EARLY -- needed to tell
            # a HUB (a listing that links to detail pages) from a DETAIL page (the
            # record itself).
            links = (extract_links(res.content, item.url, same_domain=self.same_domain)
                     if item.depth < self.max_depth else [])
            scored = self.links.score_links(item.url, links, self.intent) if links else []
            if links and self._detail_prefix is None:
                cand = self.links.detect_detail_pattern(links, self.intent)
                if cand and cand not in self._rejected_prefixes:
                    self._detail_prefix = cand
                    log.info("crawl detail-pattern detected: %s", self._detail_prefix)
            # Validate an unconfirmed prefix against this very fetch: the first
            # matching page decides — data page confirms it, anything else
            # unlearns it (and re-detection may pick the next-best cluster).
            if (self._detail_prefix and not self._detail_confirmed
                    and matches_pattern(item.url, self._detail_prefix)):
                if is_data:
                    self._detail_confirmed = True
                else:
                    log.info("crawl detail-pattern rejected (%s not a data page): %s",
                             item.url, self._detail_prefix)
                    self._rejected_prefixes.add(self._detail_prefix)
                    self._detail_prefix = None
            is_detail_page = bool(self._detail_prefix
                                  and matches_pattern(item.url, self._detail_prefix))
            has_detail_links = bool(self._detail_prefix
                                    and any(matches_pattern(ln.url, self._detail_prefix) for ln in links))
            # A hub links TO records but isn't one -> harvest its detail links,
            # don't extract it (its inline roster lacks the per-record contact
            # fields that live on the detail pages). Extract a page only when it's
            # a detail page, or a data page with no detail links to drill into.
            should_extract = is_data and (is_detail_page or not has_detail_links)

            sh = url_shape(item.url)
            shape_seen[sh] = shape_seen.get(sh, 0) + 1
            note = verdict.reason
            if should_extract:
                shape_records[sh] = shape_records.get(sh, 0) + max(verdict.estimated_records, 1)
                result.data_pages.append(DiscoveredPage(
                    url=item.url, score=max(item_score(item), 0.5), depth=item.depth,
                    estimated_records=verdict.estimated_records, reason=verdict.reason,
                    fields_found=list(verdict.fields_found),
                ))
                note = f"DATA PAGE (~{verdict.estimated_records}): {verdict.reason}"
            elif has_detail_links:
                note = f"HUB -> {self._detail_prefix}: {verdict.reason}"
            result.visited.append({"url": item.url, "status": "ok", "note": note})
            log.info("crawl ok      d%d %s -> %s", item.depth, item.url, note[:90])
            self._emit(kind="visit", seconds=secs, url=item.url,
                       status="data_page" if should_extract else "ok", note=note[:120],
                       fetched=fetched, data_pages=len(result.data_pages))

            # Record-target mode: extract this DETAIL page inline (reusing the HTML)
            # and stop the crawl if the callback says the target has been met.
            if should_extract and self.on_data_page is not None:
                try:
                    keep_going = self.on_data_page(item.url, res.content, verdict)
                except Exception as exc:  # noqa: BLE001 - extraction must not crash the crawl
                    log.warning("on_data_page failed for %s: %s", item.url, exc)
                    keep_going = True
                if keep_going is False:
                    log.info("crawl stop: record target reached after %d page(s)", fetched)
                    break

            if item.depth < self.max_depth:
                # A confirmed data/hub page's "next page" is more of the same
                # records — follow it at high priority regardless of URL shape.
                if is_data or has_detail_links:
                    for nxt in pagination_links(res.content, item.url, links):
                        frontier.push(nxt, NEXT_PAGE_SCORE, item.depth + 1,
                                      "(next page)")
                # FetchFox-style "query mode": on the seed, let the LLM propose
                # target-location URLs that aren't linked from this page (e.g.
                # other states), so the crawl isn't trapped wherever the seed
                # happens to point. Feed it the highest-scored links as pattern
                # examples so it mimics the right URL shape (the agent-roster
                # path), not nav/footer links. These match the user's explicit
                # location intent, so they get top priority (above the 0..1 range
                # used for on-page links) -- visited AND extracted first.
                if item.depth == 0 and self.client is not None:
                    sample = [ln.url for ln, s in scored if s > 0][:15] \
                        or [ln.url for ln in links[:15]]
                    proposed = self.links.propose_urls(item.url, sample, self.intent)
                    # Shuffle so no single location (the model groups them) hogs
                    # the front of the queue under a tight page budget.
                    random.shuffle(proposed)
                    for gen in proposed:
                        frontier.push(gen, PROPOSED_URL_SCORE, item.depth + 1, "(llm-proposed)")
                    if proposed:
                        log.info("crawl proposed %d URL(s) from seed: %s",
                                 len(proposed), ", ".join(proposed[:6]))
                # Hub boost (#5): if this page mainly links to record pages, its
                # outbound links are worth more.
                hub_boost = 0.3 if verdict.links_to_records else 0.0
                for ln, score in scored:
                    # Detail links (matching the detected record pattern) are the
                    # goal -- push them at top priority regardless of anchor text
                    # or keyword score (their anchor is just a name).
                    if self._detail_prefix and matches_pattern(ln.url, self._detail_prefix):
                        frontier.push(ln.url, DETAIL_LINK_SCORE, item.depth + 1, ln.anchor)
                        continue
                    if score < 0:  # negatives are excluded (login/legal/etc.)
                        continue
                    # Yield feedback (#1): boost links whose sibling shape has paid
                    # off, penalize shapes already visited that yielded nothing.
                    lsh = url_shape(ln.url)
                    if shape_records.get(lsh, 0) > 0:
                        fb = min(0.4, 0.08 * shape_records[lsh])
                    elif lsh in shape_seen:
                        fb = -0.3
                    else:
                        fb = 0.0
                    final = score + hub_boost + fb
                    if final >= 0:
                        frontier.push(ln.url, final, item.depth + 1, ln.anchor)
                # Pattern re-proposal (#4): once a non-seed page confirms a
                # productive URL shape but some target locations still have no data
                # page, re-propose URLs for that shape covering the missing ones.
                segs = self.intent.segments or self.intent.states
                if (not reproposed and is_data and item.depth > 0
                        and self.client is not None and segs):
                    found_urls = [p.url for p in result.data_pages]
                    needed = self.needed_segments() if self.needed_segments else set(segs)
                    missing = [s for s in segs
                               if s in needed and not segment_covered(s, found_urls)]
                    if missing:
                        reproposed = True
                        narrowed = self.intent.model_copy(update={"segments": missing})
                        extra = self.links.propose_urls(item.url, found_urls, narrowed)
                        for gen in extra:
                            frontier.push(gen, PROPOSED_URL_SCORE, 1, "(llm-reproposed)")
                        if extra:
                            log.info("crawl re-proposed %d URL(s) for uncovered %s: %s",
                                     len(extra), missing, ", ".join(extra[:6]))

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
            "ejected": ejected,
        }
        log.info("crawl done: fetched=%d data_pages=%d blocked=%d errors=%d llm_calls=%d",
                 fetched, len(result.data_pages), blocked, errors, result.stats["llm_calls"])
        return result


def item_score(item) -> float:
    """Frontier items carry their priority as a negated score."""
    return -getattr(item, "neg_score", -0.5)


def url_shape(url: str) -> str:
    """A coarse 'sibling' key: domain + parent path (drops the last segment).

    Groups URLs that live under the same directory (e.g. every city page under
    ``.../agent-reviews/``) so the record-yield observed on one page can boost or
    penalize its not-yet-visited siblings (the yield-feedback loop, #1).
    """
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    return f"{p.netloc.lower()}/{'/'.join(segs[:-1])}"


def segment_covered(seg: str, urls: list[str]) -> bool:
    """Heuristic: is a target segment already represented among these URLs?

    Matches a segment token as a whole path segment or a ``x-<seg>`` / ``<seg>-x``
    slug, so short codes (``in``) don't match unrelated paths (``/index``). Used
    to decide which segments still need URLs proposed (#4). Works for any segment
    value that appears in URLs (state codes, brand slugs, ...).
    """
    t = seg.strip().lower()
    if not t:
        return True
    for u in urls:
        for seg in urlparse(u).path.lower().split("/"):
            if seg == t or seg.endswith("-" + t) or seg.startswith(t + "-"):
                return True
    return False


def url_segment(url: str, segments: list[str]) -> str | None:
    """Which target segment a URL belongs to, if any (else None).

    Same whole-segment / slug match as :func:`segment_covered`, but returns the
    matching value so the crawl can skip URLs of an already-full segment.
    """
    parts = urlparse(url).path.lower().split("/")
    for s in segments:
        t = s.strip().lower()
        if not t:
            continue
        for part in parts:
            if part == t or part.endswith("-" + t) or part.startswith(t + "-"):
                return s
    return None


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
    # "blocked" covers two distinct causes -- robots.txt denial vs an anti-bot
    # block/CAPTCHA page -- and the user needs to know which one stopped them.
    robots = sum(1 for st, n in notes if st == "blocked" and "robots" in n.lower())
    antibot = sum(1 for st, n in notes if st == "blocked" and "robots" not in n.lower())
    forbidden = sum(1 for st, n in notes if "403" in n or "forbidden" in n.lower())
    timeouts = sum(1 for st, n in notes
                   if st == "error" and ("timeout" in n.lower() or "timed out" in n.lower()))
    reasons: list[str] = []
    if robots:
        reasons.append(f"{robots} URL(s) were disallowed by the site's robots.txt")
    if antibot:
        reasons.append(f"{antibot} page(s) answered with an anti-bot/CAPTCHA block "
                       "page, which this tool does not bypass")
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
