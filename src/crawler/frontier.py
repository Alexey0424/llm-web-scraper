"""Crawl frontier + link extraction helpers.

The frontier is a simple max-priority queue keyed on a link's relevance score,
with visited-deduplication and per-URL depth tracking. Link extraction pulls
same-registrable-domain ``<a href>`` targets out of a page and normalizes them
(absolute, fragment-stripped) so the classifier can score them.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from .parsing import extract_anchors


def normalize_url(url: str) -> str:
    """Drop fragments and trailing slashes for stable dedup keys."""
    url, _ = urldefrag(url.strip())
    if url.endswith("/") and url.count("/") > 3:  # keep scheme://host/ as-is
        url = url.rstrip("/")
    return url


def same_site(a: str, b: str) -> bool:
    """True if two URLs share a registrable-ish domain (last two labels)."""
    da, db = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    if not da or not db:
        return False
    return da == db or da.split(".")[-2:] == db.split(".")[-2:]


@dataclass(order=True)
class _Item:
    neg_score: float
    seq: int
    url: str = field(compare=False)
    depth: int = field(compare=False)
    anchor: str = field(compare=False, default="")


class Frontier:
    def __init__(self) -> None:
        self._heap: list[_Item] = []
        self._counter = itertools.count()
        self.visited: set[str] = set()
        self._queued: set[str] = set()

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, url: str, score: float, depth: int, anchor: str = "") -> None:
        url = normalize_url(url)
        if url in self.visited or url in self._queued:
            return
        self._queued.add(url)
        # heapq is a min-heap; negate so the highest score pops first.
        heapq.heappush(self._heap, _Item(-score, next(self._counter), url, depth, anchor))

    def pop(self) -> _Item | None:
        while self._heap:
            item = heapq.heappop(self._heap)
            self._queued.discard(item.url)
            if item.url in self.visited:
                continue
            self.visited.add(item.url)
            return item
        return None


@dataclass
class Link:
    url: str
    anchor: str


def extract_links(html: str, base_url: str, *, same_domain: bool = True) -> list[Link]:
    """Return de-duplicated same-site links with their anchor text.

    Anchors are read with Scrapling (``parsing.extract_anchors``, bs4 fallback);
    the normalization / same-site / dedup policy lives here.
    """
    seen: set[str] = set()
    out: list[Link] = []
    for href, text in extract_anchors(html):
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if same_domain and not same_site(base_url, absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(Link(url=absolute, anchor=(text or "")[:120]))
    return out
