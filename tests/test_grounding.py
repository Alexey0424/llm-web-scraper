"""Grounding guard: extracted contact values must exist in the source page.

A small local model occasionally invents a plausible email/phone/name. The
extractor must blank any email/phone/name value that cannot be located in the
fetched page (the LangExtract pattern: un-locatable extraction = hallucination),
while keeping values that appear in a different format than extracted.
"""

from __future__ import annotations

from src.crawler.freeform import FreeFormExtractor

COLS = [("Full Name", "full_name"), ("Email", "email"), ("Phone", "phone"),
        ("Source URL", "source_url")]

PAGE = """<html><body>
<div class="card">Jane Smith — call (973) 555-0101 or write jane@realco.com</div>
<div class="card">Tom Vega — call 212.555.0199</div>
</body></html>"""


class FakeLLM:
    model = "fake"
    calls = 0

    def __init__(self, records):
        self.records = records

    def available(self):
        return True

    def chat_json(self, system, user, **kw):
        return {"records": self.records}


def _extract(records):
    return FreeFormExtractor(COLS, entity="person",
                             client=FakeLLM(records)).extract(PAGE, base_url="http://x")


def test_real_values_kept_despite_format_differences():
    rows = _extract([{"full_name": "Jane Smith", "email": "jane@realco.com",
                      "phone": "973-555-0101"}])
    assert rows[0]["email"] == "jane@realco.com"
    assert rows[0]["phone"] == "973-555-0101"  # page shows (973) 555-0101


def test_hallucinated_email_is_blanked():
    rows = _extract([{"full_name": "Jane Smith", "email": "jane.smith@gmail.com",
                      "phone": "973-555-0101"}])
    assert rows[0]["email"] is None
    assert rows[0]["full_name"] == "Jane Smith"


def test_hallucinated_phone_is_blanked():
    rows = _extract([{"full_name": "Tom Vega", "email": None,
                      "phone": "305-555-7777"}])
    assert rows[0]["phone"] is None


def test_fully_invented_record_is_dropped():
    rows = _extract([{"full_name": "Bob Invented", "email": "bob@nowhere.io",
                      "phone": "999-888-7777"},
                     {"full_name": "Jane Smith", "email": None,
                      "phone": "973-555-0101"}])
    names = [r["full_name"] for r in rows]
    assert "Bob Invented" not in names
    assert "Jane Smith" in names


def test_name_variant_with_extra_token_survives():
    # "Jane M Smith" vs page's "Jane Smith": majority of tokens grounded -> keep.
    rows = _extract([{"full_name": "Jane M Smith", "email": None,
                      "phone": "973-555-0101"}])
    assert rows and rows[0]["full_name"] == "Jane M Smith"
