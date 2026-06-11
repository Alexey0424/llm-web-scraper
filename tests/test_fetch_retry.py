"""Fetch retry policy: 4xx responses must NOT be retried.

Re-hitting a 403/404 with the same fingerprint can't succeed and looks like
hammering to anti-bot systems (it worsens IP reputation and can get the whole
crawl blocked). Only transient failures — network errors and 5xx — are worth
retrying. The sitemap probe must also come AFTER the seed page, so the
crawler's first contact with a site is the URL the user gave.
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.crawler.discovery import DiscoveryCrawler
from src.crawler.intent import Planner
from src.sources.base import BaseFetcher, FetchHTTPError, FetchResult
from src.utils.rate_limit import DomainRateLimiter


class CountingFetcher(BaseFetcher):
    engine = "counting"

    def __init__(self, outcomes):
        super().__init__(limiter=DomainRateLimiter(default_delay=0,
                                                   sleep_fn=lambda s: None))
        self.backoff = 0.0
        self.outcomes = list(outcomes)  # ints = HTTP status, Exception = raise
        self.attempts = 0

    def _raw_get(self, url):
        self.attempts += 1
        out = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(out, Exception):
            raise out
        if out >= 400:
            raise FetchHTTPError(out)
        return out, "<html><body>ok</body></html>"


def test_403_is_not_retried():
    f = CountingFetcher([403])
    res = f.get("https://x.test/page")
    assert not res.ok and "403" in res.reason
    assert f.attempts == 1


def test_404_is_not_retried():
    f = CountingFetcher([404])
    res = f.get("https://x.test/page")
    assert not res.ok
    assert f.attempts == 1


def test_429_is_not_hammered():
    f = CountingFetcher([429])
    res = f.get("https://x.test/page")
    assert not res.ok
    assert f.attempts == 1


def test_5xx_is_retried_and_can_recover():
    f = CountingFetcher([500, 502, 200])
    res = f.get("https://x.test/page")
    assert res.ok
    assert f.attempts == 3


def test_network_error_is_retried():
    f = CountingFetcher([OSError("connection reset"), 200])
    res = f.get("https://x.test/page")
    assert res.ok
    assert f.attempts == 2


# ---------------------------------------------------------------------------
# Sitemap must not be the first contact with the site
# ---------------------------------------------------------------------------
class TraceFetcher:
    engine = "trace"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.order: list[str] = []

    def get(self, url: str) -> FetchResult:
        path = urlparse(url).path or "/"
        self.order.append(path)
        html = self.pages.get(path)
        if html is None:
            return FetchResult(url=url, content="", ok=False, reason="HTTP 403")
        return FetchResult(url=url, content=html, ok=True)


def test_sitemap_fetched_after_seed_not_before():
    f = TraceFetcher({
        "/": "<html><body><p>welcome</p></body></html>",
        "/sitemap.xml": '<?xml version="1.0"?><urlset></urlset>',
    })
    intent = Planner(client=None).plan("collect people", "https://x.test/")
    DiscoveryCrawler(intent, client=None, fetcher=f, max_pages=5).run("https://x.test/")
    assert "/sitemap.xml" in f.order
    assert f.order.index("/") < f.order.index("/sitemap.xml")


def test_sitemap_skipped_when_seed_is_blocked():
    f = TraceFetcher({})  # everything 403s
    intent = Planner(client=None).plan("collect people", "https://x.test/")
    DiscoveryCrawler(intent, client=None, fetcher=f, max_pages=5).run("https://x.test/")
    assert "/sitemap.xml" not in f.order  # don't poke a site that just blocked us
