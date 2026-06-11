"""Sitemap seeding: /sitemap.xml often enumerates every record page directly.

The crawler should read it (bounded, same-site, through the normal fetcher so
rate limiting applies), seed the frontier with keyword-relevant URLs, and use
the URL clusters to propose a detail pattern that the existing validation then
confirms or rejects.
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.crawler.discovery import DiscoveryCrawler
from src.crawler.intent import Planner
from src.crawler.sitemap import fetch_sitemap_urls
from src.sources.base import FetchResult

BASE = "https://site.test"


class MapFetcher:
    engine = "map"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []

    def get(self, url: str) -> FetchResult:
        path = urlparse(url).path or "/"
        self.fetched.append(path)
        html = self.pages.get(path)
        if html is None:
            return FetchResult(url=url, content="", ok=False, reason="404 not found")
        return FetchResult(url=url, content=html, ok=True)


def _urlset(*urls: str) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def _index(*sitemaps: str) -> str:
    locs = "".join(f"<sitemap><loc>{s}</loc></sitemap>" for s in sitemaps)
    return f'<?xml version="1.0"?><sitemapindex>{locs}</sitemapindex>'


def test_parses_urlset():
    f = MapFetcher({"/sitemap.xml": _urlset(f"{BASE}/a", f"{BASE}/b")})
    assert fetch_sitemap_urls(f"{BASE}/", f) == [f"{BASE}/a", f"{BASE}/b"]


def test_recurses_sitemap_index():
    f = MapFetcher({
        "/sitemap.xml": _index(f"{BASE}/sm-agents.xml", f"{BASE}/sm-blog.xml"),
        "/sm-agents.xml": _urlset(f"{BASE}/agents/jane"),
        "/sm-blog.xml": _urlset(f"{BASE}/blog/post-1"),
    })
    urls = fetch_sitemap_urls(f"{BASE}/", f)
    assert f"{BASE}/agents/jane" in urls
    assert f"{BASE}/blog/post-1" in urls


def test_filters_offsite_and_caps():
    f = MapFetcher({"/sitemap.xml": _urlset(
        "https://evil.test/x", *[f"{BASE}/p{i}" for i in range(40)])})
    urls = fetch_sitemap_urls(f"{BASE}/", f, max_urls=10)
    assert len(urls) == 10
    assert all(u.startswith(BASE) for u in urls)


def test_missing_sitemap_returns_empty():
    f = MapFetcher({})
    assert fetch_sitemap_urls(f"{BASE}/", f) == []


_PROFILE_LD = """<script type="application/ld+json">
{{"@type": "Person", "name": "{name}", "telephone": "973-555-010{n}"}}
</script>"""


def test_discovery_uses_sitemap_to_reach_unlinked_record_pages():
    # The seed page links to nothing useful; only the sitemap knows the
    # /profile/* record pages. The crawl must still find them.
    pages = {
        "/": "<html><body><p>Welcome. Nothing linked here.</p></body></html>",
        "/sitemap.xml": _urlset(
            f"{BASE}/profile/maria", f"{BASE}/profile/david",
            f"{BASE}/profile/priya", f"{BASE}/privacy"),
    }
    for i, p in enumerate(("maria", "david", "priya")):
        pages[f"/profile/{p}"] = ("<html><head>"
                                  + _PROFILE_LD.format(name=p.title(), n=i)
                                  + f"</head><body><h1>{p.title()}</h1></body></html>")
    fetcher = MapFetcher(pages)
    intent = Planner(client=None).plan("collect agents", f"{BASE}/")
    crawler = DiscoveryCrawler(intent, client=None, fetcher=fetcher, max_pages=10)
    result = crawler.run(f"{BASE}/")
    found = set(result.data_page_urls)
    assert {f"{BASE}/profile/maria", f"{BASE}/profile/david",
            f"{BASE}/profile/priya"} <= found


def test_discovery_survives_sites_without_sitemap():
    pages = {"/": "<html><body><p>hi</p></body></html>"}
    fetcher = MapFetcher(pages)
    intent = Planner(client=None).plan("collect agents", f"{BASE}/")
    result = DiscoveryCrawler(intent, client=None, fetcher=fetcher,
                              max_pages=5).run(f"{BASE}/")
    assert result.stats["pages_fetched"] >= 1
