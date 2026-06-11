"""Regression tests for silent errors found in the 2026-06 audit.

Each test documents a failure mode that previously lost or corrupted data
without raising: blocked-page false positives, JSON-LD merge collapse on
listing pages, page-level contact spraying, misleading diagnoses, and the
export ledger flagging a run against its own export file.
"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from src.crawler.classifier import PageClassifier
from src.crawler.discovery import CrawlResult, diagnose
from src.crawler.fields import derive_columns
from src.crawler.freeform import FreeFormExtractor
from src.crawler.intent import CrawlIntent
from src.sources.base import detect_blocked_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeLLM:
    """Offline OllamaClient stand-in returning canned extraction records."""

    def __init__(self, records):
        self._records = records
        self.model = "fake"
        self.calls = 0
        self.on_call = None

    def available(self) -> bool:
        return True

    def chat_json(self, system, user, **kw):
        self.calls += 1
        return {"records": [dict(r) for r in self._records]}


COLS = derive_columns(["Full Name", "Email", "Phone"])

AGENTS = [
    {"full_name": "Ann Lee", "email": None, "phone": None},
    {"full_name": "Bob Diaz", "email": "bob@x.com", "phone": None},
    {"full_name": "Cara Fox", "email": None, "phone": "201-555-0101"},
]

BREADCRUMB_LD = """<script type="application/ld+json">
{"@context":"http://schema.org","@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":1,"name":"New Jersey",
  "item":"https://site.test/agents/nj/"},
 {"@type":"ListItem","position":2,"name":"Newark",
  "item":"https://site.test/agents/newark-nj/"}]}
</script>"""

ORG_FOOTER_LD = """<script type="application/ld+json">
{"@context":"http://schema.org","@type":"Organization","name":"Acme Realty Group",
 "telephone":"(555) 111-2222","url":"https://site.test"}
</script>"""

PERSON_DETAIL_LD = """<script type="application/ld+json">
{"@context":"http://schema.org","@type":"LocalBusiness","name":"Dana Rae Moreno",
 "telephone":"(609) 555-0143","address":{"@type":"PostalAddress",
 "addressLocality":"Princeton","addressRegion":"NJ"}}
</script>"""


def _page(*parts: str) -> str:
    return "<html><head></head><body>" + "".join(parts) + "</body></html>"


# ---------------------------------------------------------------------------
# 1. detect_blocked_page: benign reCAPTCHA assets must not kill the page
# ---------------------------------------------------------------------------
def test_recaptcha_script_asset_is_not_a_block_page():
    html = _page(
        "<h1>Our Agents</h1><p>Meet the team.</p>" * 30,
        '<script src="https://www.google.com/recaptcha/api.js" async></script>',
        '<div class="g-recaptcha" data-sitekey="abc123"></div>',
    )
    assert detect_blocked_page(html) is None


def test_recaptcha_badge_text_is_not_a_block_page():
    html = _page(
        "<h1>Contact our brokerage</h1>" + "<p>Agent roster and offices.</p>" * 40,
        "<footer><small>This site is protected by reCAPTCHA and the Google "
        "Privacy Policy applies.</small></footer>",
    )
    assert detect_blocked_page(html) is None


def test_real_challenge_page_is_still_detected():
    html = _page("<h1>Please verify you are a human</h1><p>Complete the "
                 "CAPTCHA below to continue.</p>")
    assert detect_blocked_page(html) is not None


def test_access_denied_page_is_still_detected():
    html = _page("<h1>Access Denied</h1><p>You don't have permission.</p>")
    assert detect_blocked_page(html) is not None


def test_empty_response_is_still_detected():
    assert detect_blocked_page("") == "empty response"


# ---------------------------------------------------------------------------
# 2. FreeFormExtractor: JSON-LD merge collapse / contact spraying
# ---------------------------------------------------------------------------
def test_listing_with_breadcrumb_jsonld_keeps_all_records():
    """Roster page whose only JSON-LD is breadcrumbs ("New Jersey", ...).

    The breadcrumb name must NOT be mistaken for a single-entity detail page,
    which previously collapsed the whole roster into one merged record.
    """
    html = _page(BREADCRUMB_LD, "<ul><li>Ann Lee</li><li>Bob Diaz</li>"
                 "<li>Cara Fox</li></ul>")
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(AGENTS))
    out = ex.extract(html, base_url="https://site.test/agents/newark-nj/")
    assert len(out) == 3
    names = {r["full_name"] for r in out}
    assert names == {"Ann Lee", "Bob Diaz", "Cara Fox"}


def test_listing_with_org_footer_jsonld_keeps_all_records():
    """Sitewide Organization JSON-LD must not collapse a listing to one row."""
    html = _page(ORG_FOOTER_LD, "<ul><li>Ann Lee</li><li>Bob Diaz</li>"
                 "<li>Cara Fox</li></ul>")
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(AGENTS))
    out = ex.extract(html, base_url="https://site.test/agents/")
    assert len(out) == 3


def test_office_phone_not_sprayed_across_listing_records():
    """The org's office phone must not be copied into every agent's row.

    Shared page-level contact on a multi-record page creates plausible-but-
    wrong data, and later collapses distinct people into one during phone-key
    dedup.
    """
    html = _page(ORG_FOOTER_LD, "<ul><li>Ann Lee</li><li>Bob Diaz</li>"
                 "<li>Cara Fox</li></ul>")
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(AGENTS))
    out = ex.extract(html, base_url="https://site.test/agents/")
    assert sum(1 for r in out if r.get("phone") == "555-111-2222") == 0


def test_page_mailto_not_sprayed_across_listing_records():
    html = _page('<a href="mailto:info@office.com">Contact us</a>',
                 "<ul><li>Ann Lee</li><li>Bob Diaz</li><li>Cara Fox</li></ul>")
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(AGENTS))
    out = ex.extract(html, base_url="https://site.test/agents/")
    assert sum(1 for r in out if r.get("email") == "info@office.com") <= 1


def test_detail_page_still_merges_duplicate_rows_and_fills_contact():
    """Regression-keep: a true detail page still merges LLM name variants into
    ONE record and fills phone from JSON-LD (the original purpose)."""
    html = _page(PERSON_DETAIL_LD, "<h1>Dana Rae Moreno</h1>",
                 '<a href="mailto:dana@compass.com">Email me</a>')
    rows = [
        {"full_name": "Dana Rae Moreno", "email": None, "phone": None},
        {"full_name": "Dana Moreno", "email": None, "phone": None},
    ]
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(rows))
    out = ex.extract(html, base_url="https://site.test/profile/dana")
    assert len(out) == 1
    assert out[0]["phone"] == "609-555-0143"
    assert out[0]["email"] == "dana@compass.com"


def test_single_record_page_contact_fill_still_works():
    """One extracted row + page-level mailto/tel: filling is still correct."""
    html = _page('<a href="mailto:ann@x.com">mail</a>',
                 '<a href="tel:2015550101">call</a>', "<h1>Ann Lee</h1>")
    rows = [{"full_name": "Ann Lee", "email": None, "phone": None}]
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM(rows))
    out = ex.extract(html, base_url="https://site.test/profile/ann")
    assert len(out) == 1
    assert out[0]["email"] == "ann@x.com"
    assert out[0]["phone"] == "201-555-0101"


def test_breadcrumbs_alone_do_not_fabricate_a_record():
    """A page with breadcrumb JSON-LD and no extractable records must yield
    nothing — not a bogus record named "New Jersey"."""
    html = _page(BREADCRUMB_LD, "<p>Nothing here.</p>")
    ex = FreeFormExtractor(COLS, entity="real estate agent", client=FakeLLM([]))
    out = ex.extract(html, base_url="https://site.test/agents/nj/")
    assert out == []


# ---------------------------------------------------------------------------
# 3. PageClassifier heuristic: sitewide org JSON-LD isn't a data page
# ---------------------------------------------------------------------------
def test_org_footer_jsonld_does_not_make_every_page_a_data_page():
    html = _page(ORG_FOOTER_LD, "<h1>Privacy policy</h1><p>Legal text.</p>")
    v = PageClassifier(None).classify("https://site.test/privacy", html,
                                      CrawlIntent())
    assert v.is_data_page is False


def test_localbusiness_detail_jsonld_still_classifies_as_data_page():
    # Zillow-style: an individual agent marked up as LocalBusiness.
    html = _page(PERSON_DETAIL_LD, "<h1>Dana Rae Moreno</h1>")
    v = PageClassifier(None).classify("https://site.test/profile/dana", html,
                                      CrawlIntent())
    assert v.is_data_page is True


# ---------------------------------------------------------------------------
# 4. diagnose(): anti-bot blocks must not be reported as robots.txt
# ---------------------------------------------------------------------------
def _result_with(visited, **stats) -> CrawlResult:
    r = CrawlResult(intent=CrawlIntent())
    r.visited = visited
    base = {"pages_fetched": 0, "blocked": 0, "errors": 0, "data_pages_found": 0}
    base.update(stats)
    r.stats = base
    return r


def test_diagnose_reports_antibot_blocks_as_antibot():
    r = _result_with(
        [{"url": "u", "status": "blocked",
          "note": "blocked page detected: 'captcha'"}],
        blocked=1)
    msg = diagnose(r).lower()
    assert "robots.txt" not in msg
    assert "anti-bot" in msg or "block" in msg


def test_diagnose_still_reports_robots_blocks_as_robots():
    r = _result_with(
        [{"url": "u", "status": "blocked",
          "note": "Disallowed by robots.txt"}],
        blocked=1)
    assert "robots.txt" in diagnose(r).lower()


# ---------------------------------------------------------------------------
# 5. Export ledger: a run must not be flagged against its own export
# ---------------------------------------------------------------------------
@pytest.fixture
def web_env(tmp_path, monkeypatch):
    from src.crawler import dedup
    from src.web import app as app_mod

    monkeypatch.setattr(app_mod, "UI_FF_DB", tmp_path / "ui.sqlite")
    monkeypatch.setattr(app_mod, "EXPORTS_DIR", tmp_path / "exports")
    (tmp_path / "exports").mkdir()
    dedup.clear_ledger_cache()
    flask_app = app_mod.create_app()
    flask_app.config.update(TESTING=True)
    yield app_mod, tmp_path, flask_app.test_client()
    dedup.clear_ledger_cache()


def _seed_run_with_export(app_mod, tmp_path, started_iso: str, export_mtime: float):
    from src.crawler.store import FreeFormStore
    from src.exporters import dynamic_exporter

    cols = [("Name", "name"), ("Email", "email"), ("Source URL", "source_url")]
    s = FreeFormStore(tmp_path / "ui.sqlite")
    rid = s.start_run("https://a.com", "agents: name, email", cols, started_iso)
    s.add_record(rid, "ann@x.com", "https://a.com/p",
                 {"name": "Ann Lee", "email": "ann@x.com"}, started_iso)
    s.close()
    path = tmp_path / "exports" / "agents_test.xlsx"
    dynamic_exporter.write_xlsx(cols, [{"name": "Ann Lee", "email": "ann@x.com",
                                        "source_url": "https://a.com/p"}], path)
    os.utime(path, (export_mtime, export_mtime))
    return rid


def test_run_not_flagged_by_export_written_after_it_started(web_env):
    app_mod, tmp_path, client = web_env
    now = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 300))
    # Export written AFTER the run started (i.e. the run's own auto-export).
    _seed_run_with_export(app_mod, tmp_path, started, export_mtime=now)
    page = client.get("/").get_data(as_text=True)
    assert "already saved in a previous export" not in page


def test_run_flagged_by_genuinely_previous_export(web_env):
    app_mod, tmp_path, client = web_env
    now = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 300))
    # Export predates the run -> genuinely "previously saved".
    _seed_run_with_export(app_mod, tmp_path, started, export_mtime=now - 3600)
    page = client.get("/").get_data(as_text=True)
    assert "already saved in a previous export" in page


# ---------------------------------------------------------------------------
# 6. /reset and /delete-run refuse to run while a crawl is running
# ---------------------------------------------------------------------------
def test_reset_refused_while_crawl_running(web_env):
    app_mod, tmp_path, client = web_env
    with app_mod._crawl_lock:
        prev = app_mod._crawl["state"]
        app_mod._crawl["state"] = "running"
    try:
        assert client.post("/reset").status_code == 409
        assert client.post("/clear-history").status_code == 409
    finally:
        with app_mod._crawl_lock:
            app_mod._crawl["state"] = prev


# ---------------------------------------------------------------------------
# 7. OllamaClient.available(): family-only match warns about missing tag
# ---------------------------------------------------------------------------
def test_available_family_match_warns(caplog, monkeypatch):
    from src.crawler.llm import OllamaClient

    client = OllamaClient(host="http://fake:1", model="gemma4:e4b")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "gemma4:26b"}]}

    class _Http:
        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(client, "_client", _Http())
    with caplog.at_level(logging.WARNING, logger="src.crawler.llm"):
        assert client.available() is True
    assert any("not installed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. html_to_signal_text: default cap covers what the extractor can consume
# ---------------------------------------------------------------------------
def test_signal_text_default_limit_covers_extractor_chunks():
    from src.extractors.llm_extractor import _chunks, html_to_signal_text

    body = " ".join(f"Agent Number {i} [email: a{i}@x.com]" for i in range(2000))
    text = html_to_signal_text(f"<html><body><p>{body}</p></body></html>")
    # 4 chunks of 6000 with 400 overlap consume up to 22800 chars; the default
    # cap must not silently starve them at 12000.
    assert len(text) >= 20000
    assert len(_chunks(text)) >= 4
