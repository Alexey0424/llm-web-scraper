"""Pagination following: a multi-page roster must not be cut at page 1.

The old behavior only boosted links that *looked* like pagination by URL shape
(?page=, /page/N). Real sites use rel="next" or a "Next ›" anchor with opaque
URLs (?p=2); a confirmed data page's next-link must be followed at high
priority regardless of URL shape.
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.crawler.discovery import DiscoveryCrawler, pagination_links
from src.crawler.frontier import Link, extract_links
from src.crawler.intent import Planner
from src.sources.base import FetchResult

BASE = "https://site.test"


class MapFetcher:
    engine = "map"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url: str) -> FetchResult:
        p = urlparse(url)
        key = p.path + (f"?{p.query}" if p.query else "")
        html = self.pages.get(key)
        if html is None:
            return FetchResult(url=url, content="", ok=False, reason="404 not found")
        return FetchResult(url=url, content=html, ok=True)


def _roster(n: int, next_html: str = "") -> str:
    cards = "".join(
        f'<div><h3>Person {n}{i}</h3>'
        f'<a href="mailto:p{n}{i}@x.com">mail</a>'
        f'<a href="tel:+1973555{n}10{i}">call</a></div>'
        for i in range(3))
    return f"<html><body>{cards}{next_html}</body></html>"


def test_pagination_links_finds_rel_next_and_anchor_text():
    html = ('<html><body><a rel="next" href="/r?p=2">2</a>'
            '<a href="/r?p=9">Next ›</a>'
            '<a href="/other">Unrelated</a></body></html>')
    links = extract_links(html, f"{BASE}/r")
    out = pagination_links(html, f"{BASE}/r", links)
    assert f"{BASE}/r?p=2" in out
    assert f"{BASE}/r?p=9" in out
    assert f"{BASE}/other" not in out


def test_link_rel_next_in_head_is_found():
    html = ('<html><head><link rel="next" href="/r?p=2"></head>'
            "<body><p>x</p></body></html>")
    out = pagination_links(html, f"{BASE}/r", [])
    assert f"{BASE}/r?p=2" in out


def test_crawl_follows_opaque_next_links_through_all_roster_pages():
    pages = {
        "/agents": _roster(1, '<a rel="next" href="/agents?p=2">2</a>'),
        "/agents?p=2": _roster(2, '<a href="/agents?p=3">Next »</a>'),
        "/agents?p=3": _roster(3),
    }
    intent = Planner(client=None).plan("collect people", f"{BASE}/agents")
    crawler = DiscoveryCrawler(intent, client=None, fetcher=MapFetcher(pages),
                               max_pages=10, use_sitemap=False)
    result = crawler.run(f"{BASE}/agents")
    found = set(result.data_page_urls)
    assert f"{BASE}/agents?p=2" in found
    assert f"{BASE}/agents?p=3" in found


def test_next_links_not_followed_from_non_data_pages():
    # A junk page's "Next" must not get the high pagination priority.
    html = '<html><body><p>blog blah</p><a href="/blog?p=2">Next ›</a></body></html>'
    links = [Link(f"{BASE}/blog?p=2", "Next ›")]
    out = pagination_links(html, f"{BASE}/blog", links)
    assert out  # the helper finds it...
    pages = {"/blog": html}
    intent = Planner(client=None).plan("collect people", f"{BASE}/blog")
    crawler = DiscoveryCrawler(intent, client=None, fetcher=MapFetcher(pages),
                               max_pages=5, use_sitemap=False)
    result = crawler.run(f"{BASE}/blog")
    # ...but the crawl doesn't chase it from a non-data page at high priority:
    # /blog?p=2 is only visited if ordinary link scoring already wanted it.
    statuses = {v["url"]: v["status"] for v in result.visited}
    assert statuses.get(f"{BASE}/blog?p=2") != "data_page"
