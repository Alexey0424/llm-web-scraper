"""URL canonicalization: the same page must produce ONE frontier key.

Tracking params and query-order differences make one page look like many URLs;
each duplicate costs a full fetch out of the page budget (~80s/page on real
runs). Lightweight, deterministic subset of URL-pattern dedup: strip tracking
params, sort the rest, lowercase scheme/host.
"""

from __future__ import annotations

from src.crawler.frontier import Frontier, normalize_url

BASE = "https://site.test"


def test_tracking_params_stripped():
    assert normalize_url(f"{BASE}/p?utm_source=tw&utm_campaign=x&id=3") == f"{BASE}/p?id=3"
    assert normalize_url(f"{BASE}/p?gclid=abc") == f"{BASE}/p"
    assert normalize_url(f"{BASE}/p?fbclid=zzz&page=2") == f"{BASE}/p?page=2"


def test_query_params_sorted():
    assert normalize_url(f"{BASE}/p?b=2&a=1") == normalize_url(f"{BASE}/p?a=1&b=2")


def test_host_and_scheme_lowercased_path_case_kept():
    assert normalize_url("HTTPS://Site.TEST/Agents/Jane") == "https://site.test/Agents/Jane"


def test_fragment_and_trailing_slash_still_normalized():
    assert normalize_url(f"{BASE}/x/#frag") == f"{BASE}/x"
    assert normalize_url(f"{BASE}/") == f"{BASE}/"  # bare host keeps its slash


def test_frontier_treats_tracking_variants_as_one_url():
    f = Frontier()
    f.push(f"{BASE}/p?id=3&utm_source=tw", 0.5, 1)
    f.push(f"{BASE}/p?utm_campaign=y&id=3", 0.9, 1)
    assert f.pop() is not None
    assert f.pop() is None  # the variant was recognized as the same page


def test_meaningful_params_preserved():
    assert normalize_url(f"{BASE}/list?page=2") == f"{BASE}/list?page=2"
    assert normalize_url(f"{BASE}/s?q=jane+smith") == f"{BASE}/s?q=jane+smith"
