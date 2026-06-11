"""html_to_signal_text must preserve record boundaries (block structure).

Flattening a whole page to one line destroys the visual grouping that tells
the LLM which phone belongs to which name; block-level elements must become
line breaks so each record card reads as its own group of lines.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.llm_extractor import html_to_signal_text

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_records_do_not_share_a_line():
    html = (FIXTURES / "brokerage_static.html").read_text(encoding="utf-8")
    text = html_to_signal_text(html)
    lines = text.splitlines()
    assert len(lines) > 3, "page flattened to a single line"
    aaron_lines = [ln for ln in lines if "Aaron Hazel" in ln]
    assert aaron_lines, "name lost"
    # Another record's contact info must not share Aaron's line.
    assert all("adam@icongrouprealtors.com" not in ln for ln in aaron_lines)
    assert all("Adam Hopkins" not in ln for ln in aaron_lines)


def test_contact_markers_stay_near_their_record():
    html = (FIXTURES / "brokerage_static.html").read_text(encoding="utf-8")
    lines = html_to_signal_text(html).splitlines()
    name_i = next(i for i, ln in enumerate(lines) if "Aaron Hazel" in ln)
    email_i = next(i for i, ln in enumerate(lines)
                   if "aaron.hazel@talktotucker.com" in ln)
    other_i = next(i for i, ln in enumerate(lines) if "Adam Hopkins" in ln)
    assert name_i <= email_i < other_i  # Aaron's email between his name and Adam


def test_no_blank_lines_or_trailing_space():
    html = (FIXTURES / "brokerage_static.html").read_text(encoding="utf-8")
    for ln in html_to_signal_text(html).splitlines():
        assert ln == ln.strip() and ln != ""


def test_limit_still_truncates():
    html = (FIXTURES / "brokerage_static.html").read_text(encoding="utf-8")
    assert len(html_to_signal_text(html, limit=100)) <= 100


def test_br_breaks_lines():
    text = html_to_signal_text("<p>Jane Smith<br>555-111-2222</p>")
    assert "Jane Smith" in text.splitlines()[0]
    assert any("555-111-2222" in ln for ln in text.splitlines()[1:])
