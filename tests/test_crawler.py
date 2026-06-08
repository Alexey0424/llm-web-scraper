"""Offline tests for the Phase-1 discovery crawler.

No network and no LLM: the crawler is heuristic-first, so its frontier, link
extraction, keyword scoring, page classification and the end-to-end discovery
loop are all exercised deterministically. The LLM-only paths (intent planning,
link re-ranking, extraction) degrade to these heuristics when no client is
passed, which is exactly what these tests assert.
"""

from pathlib import Path
from urllib.parse import urlparse

from src.crawler.classifier import LinkClassifier, PageClassifier, _kw_score
from src.crawler.discovery import DiscoveryCrawler
from src.crawler.frontier import Frontier, extract_links, normalize_url, same_site
from src.crawler.glue import result_to_source_cfg
from src.crawler.intent import CrawlIntent, Planner
from src.extractors.llm_extractor import html_to_signal_text, _clean
from src.sources.base import FetchResult

FIX = Path(__file__).resolve().parent / "fixtures" / "crawl_site"
BASE = "https://greenfield.test"


# ---------------------------------------------------------------------------
# Fetcher that maps a URL path onto a fixture file (no network).
# ---------------------------------------------------------------------------
class DirFetcher:
    engine = "dir-fixture"

    def __init__(self, directory: Path, base: str = BASE):
        self.dir = directory
        self.base = base

    def get(self, url: str) -> FetchResult:
        path = urlparse(url).path.lstrip("/") or "index.html"
        # Strip query (?page=2) to resolve the underlying file.
        f = self.dir / path
        if not f.exists():
            return FetchResult(url=url, content="", ok=False, reason="404 not found")
        return FetchResult(url=url, content=f.read_text(encoding="utf-8"), ok=True)


# ---------------------------------------------------------------------------
# Frontier
# ---------------------------------------------------------------------------
def test_frontier_pops_highest_score_first():
    f = Frontier()
    f.push(f"{BASE}/low", 0.1, 1)
    f.push(f"{BASE}/high", 0.9, 1)
    f.push(f"{BASE}/mid", 0.5, 1)
    order = [f.pop().url, f.pop().url, f.pop().url]
    assert order == [f"{BASE}/high", f"{BASE}/mid", f"{BASE}/low"]


def test_frontier_dedups_visited_and_queued():
    f = Frontier()
    f.push(f"{BASE}/a", 0.5, 0)
    f.push(f"{BASE}/a", 0.9, 0)  # duplicate while queued -> ignored
    first = f.pop()
    assert first.url == f"{BASE}/a"
    f.push(f"{BASE}/a", 1.0, 0)  # already visited -> ignored
    assert f.pop() is None


def test_normalize_and_same_site():
    assert normalize_url(f"{BASE}/x/#frag") == f"{BASE}/x"
    assert same_site(f"{BASE}/a", "https://www.greenfield.test/b")
    assert not same_site(BASE, "https://evil.test/b")


# ---------------------------------------------------------------------------
# Link extraction + keyword scoring
# ---------------------------------------------------------------------------
def test_extract_links_same_domain_only_and_skips_non_http():
    html = (FIX / "index.html").read_text(encoding="utf-8")
    links = extract_links(html, f"{BASE}/index.html")
    urls = {l.url for l in links}
    assert f"{BASE}/our-agents.html" in urls
    assert all(u.startswith(BASE) for u in urls)
    assert not any("mailto:" in u or "tel:" in u for u in urls)


def test_keyword_scoring_prefers_rosters_excludes_noise():
    intent = CrawlIntent()
    assert _kw_score("/our-agents", intent.include_keywords, intent.exclude_keywords) > 0
    assert _kw_score("/careers", intent.include_keywords, intent.exclude_keywords) < 0
    assert _kw_score("/privacy", intent.include_keywords, intent.exclude_keywords) < 0


def test_link_classifier_ranks_agent_link_above_noise():
    html = (FIX / "index.html").read_text(encoding="utf-8")
    links = extract_links(html, f"{BASE}/index.html")
    scored = LinkClassifier(client=None).score_links(f"{BASE}/index.html", links, CrawlIntent())
    top = scored[0][0].url
    assert top == f"{BASE}/our-agents.html"
    # careers/privacy/login must be scored negative (excluded).
    neg = {l.url for l, s in scored if s < 0}
    assert f"{BASE}/careers.html" in neg
    assert f"{BASE}/privacy.html" in neg


# ---------------------------------------------------------------------------
# Page classification (heuristic: contact-link density)
# ---------------------------------------------------------------------------
def test_page_classifier_flags_roster_not_careers():
    pc = PageClassifier(client=None)
    roster = (FIX / "our-agents.html").read_text(encoding="utf-8")
    careers = (FIX / "careers.html").read_text(encoding="utf-8")
    assert pc.classify(f"{BASE}/our-agents.html", roster, CrawlIntent()).is_data_page
    assert not pc.classify(f"{BASE}/careers.html", careers, CrawlIntent()).is_data_page


# ---------------------------------------------------------------------------
# End-to-end discovery loop (no LLM)
# ---------------------------------------------------------------------------
def test_discovery_crawl_finds_roster_pages():
    intent = Planner(client=None).plan("real estate agents in NJ", f"{BASE}/index.html", ["NJ"])
    crawler = DiscoveryCrawler(intent, client=None, fetcher=DirFetcher(FIX),
                               max_pages=20, max_depth=3)
    result = crawler.run(f"{BASE}/index.html")
    found = set(result.data_page_urls)
    assert f"{BASE}/our-agents.html" in found
    # Pagination should be followed to page 2.
    assert any("our-agents-2.html" in u for u in found)
    # Noise pages must not be flagged as data pages.
    assert f"{BASE}/careers.html" not in found
    assert f"{BASE}/privacy.html" not in found
    assert result.stats["pages_fetched"] >= 3


def test_discovery_crawl_ejects_when_should_stop():
    # Cooperative cancellation: should_stop() True -> the loop breaks before any
    # fetch, and the result is flagged ejected (partial results are kept upstream).
    intent = Planner(client=None).plan("agents", f"{BASE}/index.html", ["NJ"])
    crawler = DiscoveryCrawler(intent, client=None, fetcher=DirFetcher(FIX),
                               max_pages=20, should_stop=lambda: True)
    result = crawler.run(f"{BASE}/index.html")
    assert result.stats["pages_fetched"] == 0
    assert result.stats["ejected"] is True


def test_result_to_source_cfg_shape():
    intent = Planner(client=None).plan("agents", f"{BASE}/index.html", ["NJ", "PA"])
    crawler = DiscoveryCrawler(intent, client=None, fetcher=DirFetcher(FIX), max_pages=20)
    cfg = result_to_source_cfg(crawler.run(f"{BASE}/index.html"))
    assert cfg["extractor"] == "llm"
    assert cfg["enabled"] is True
    assert set(cfg["pages"].keys()) == {"NJ", "PA"}
    assert all(u.startswith(BASE) for u in cfg["pages"]["NJ"])


# ---------------------------------------------------------------------------
# LLM extractor pure helpers (no model call)
# ---------------------------------------------------------------------------
def test_signal_text_inlines_email_and_phone():
    html = (FIX / "our-agents.html").read_text(encoding="utf-8")
    text = html_to_signal_text(html)
    assert "[email: maria.gonzalez@greenfieldrealty.com]" in text
    assert "[phone: +19735550101]" in text


def test_clean_normalizes_nulls():
    assert _clean("  John   Doe ") == "John Doe"
    assert _clean("null") is None
    assert _clean("N/A") is None
    assert _clean(None) is None
