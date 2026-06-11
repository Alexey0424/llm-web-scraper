"""Detail-pattern validation: a learned record-URL prefix must prove itself.

The prefix is induced from the FIRST page with links (often the seed). If the
seed's largest URL cluster is NOT the record pattern (city pages, categories),
every wrong-prefix link gets top priority and drains the page budget. The
crawler must: (1) unlearn a prefix whose first fetched match is not a data
page, (2) never re-learn a rejected prefix, (3) demote queued top-priority
URLs of a rejected prefix, and (4) re-detect the real pattern later.
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.crawler.discovery import DiscoveryCrawler
from src.crawler.intent import Planner
from src.sources.base import FetchResult

BASE = "https://site.test"

_PROFILE_LD = """<script type="application/ld+json">
{{"@type": "Person", "name": "{name}", "telephone": "973-555-{n}"}}
</script>"""


def _page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _links(*paths: str) -> str:
    return " ".join(f'<a href="{p}">{p.rsplit("/", 1)[-1]}</a>' for p in paths)


class MapFetcher:
    engine = "map"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url: str) -> FetchResult:
        html = self.pages.get(urlparse(url).path or "/")
        if html is None:
            return FetchResult(url=url, content="", ok=False, reason="404 not found")
        return FetchResult(url=url, content=html, ok=True)


def _site() -> dict[str, str]:
    """Seed's only URL cluster (/city/*) is junk; the real records are under
    /profile/*, reachable via an 'our agents' roster page."""
    pages = {
        "/": _page(_links("/city/newark", "/city/edison", "/city/trenton")
                   + ' <a href="/agents">Our agents directory</a>'),
        "/agents": _page(
            '<a href="mailto:a@x.com">a</a><a href="mailto:b@x.com">b</a>'
            '<a href="mailto:c@x.com">c</a>'
            + _links("/profile/maria", "/profile/david", "/profile/priya")),
    }
    for p in ("newark", "edison", "trenton"):
        pages[f"/city/{p}"] = _page(f"<p>Things to do in {p}. Tourism info.</p>")
    for i, p in enumerate(("maria", "david", "priya")):
        pages[f"/profile/{p}"] = _page(
            f"<h1>{p.title()} Realtor</h1>",
            head=_PROFILE_LD.format(name=p.title(), n=f"010{i}"))
    return pages


def _run(max_pages: int = 20):
    intent = Planner(client=None).plan("real estate agents", f"{BASE}/")
    crawler = DiscoveryCrawler(intent, client=None, fetcher=MapFetcher(_site()),
                               max_pages=max_pages, max_depth=3)
    return crawler, crawler.run(f"{BASE}/")


def test_wrong_prefix_is_unlearned_and_real_one_relearned():
    crawler, result = _run()
    assert crawler._detail_prefix == "/profile"
    found = set(result.data_page_urls)
    assert {f"{BASE}/profile/maria", f"{BASE}/profile/david",
            f"{BASE}/profile/priya"} <= found
    # The junk cluster must not be recorded as data pages.
    assert not any("/city/" in u for u in found)


def test_rejected_prefix_not_relearned():
    crawler, _ = _run()
    assert "/city" in crawler._rejected_prefixes


def test_queued_wrong_prefix_urls_are_demoted():
    # After /city is rejected, the remaining queued /city/* (pushed at top
    # priority) must NOT be fetched before the roster page.
    _, result = _run()
    order = [v["url"] for v in result.visited]
    agents_i = order.index(f"{BASE}/agents")
    later_city = [u for u in order if "/city/" in u and u != f"{BASE}/city/newark"]
    assert all(order.index(u) > agents_i for u in later_city)
