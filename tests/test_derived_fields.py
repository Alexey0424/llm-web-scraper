"""Derived-field post-pass: name splitting/joining, JSON-LD org -> agency,
URL-only columns folding into Source URL, and email normalization."""

from __future__ import annotations

from src.crawler.fields import derive_columns
from src.crawler.freeform import FreeFormExtractor
from src.crawler.jsonld import extract_jsonld_fields


class FakeLLM:
    model = "fake"
    calls = 0

    def __init__(self, records):
        self.records = records

    def available(self):
        return True

    def chat_json(self, system, user, **kw):
        return {"records": self.records}


COLS = [("Full Name", "full_name"), ("First Name", "first_name"),
        ("Last Name", "last_name"), ("Email", "email"),
        ("Source URL", "source_url")]


def _extract(records, page):
    return FreeFormExtractor(COLS, entity="person",
                             client=FakeLLM(records)).extract(page, base_url="http://x")


def test_first_last_derived_from_full_name():
    page = "<html><body><div>Jane Smith</div></body></html>"
    rows = _extract([{"full_name": "Jane Smith", "first_name": None,
                      "last_name": None, "email": None}], page)
    assert rows[0]["first_name"] == "Jane"
    assert rows[0]["last_name"] == "Smith"


def test_middle_initial_not_part_of_last_name():
    page = "<html><body><div>Maria J DePasquale</div></body></html>"
    rows = _extract([{"full_name": "Maria J DePasquale", "first_name": None,
                      "last_name": None, "email": None}], page)
    assert rows[0]["first_name"] == "Maria"
    assert rows[0]["last_name"] == "DePasquale"


def test_multiword_last_name_kept_whole():
    page = "<html><body><div>Dana Rae Moreno</div></body></html>"
    rows = _extract([{"full_name": "Dana Rae Moreno", "first_name": None,
                      "last_name": None, "email": None}], page)
    assert rows[0]["first_name"] == "Dana"
    assert rows[0]["last_name"] == "Rae Moreno"


def test_full_name_joined_from_first_and_last():
    page = "<html><body><div>Jane Smith</div></body></html>"
    rows = _extract([{"full_name": None, "first_name": "Jane",
                      "last_name": "Smith", "email": None}], page)
    assert rows[0]["full_name"] == "Jane Smith"


def test_agency_name_column_is_not_treated_as_person_name():
    page = "<html><body><div>Jane Smith of Acme Realty</div></body></html>"
    cols = COLS[:3] + [("Agency Name", "agency_name")] + COLS[3:]
    rows = FreeFormExtractor(cols, entity="person", client=FakeLLM(
        [{"full_name": None, "first_name": "Jane", "last_name": "Smith",
          "agency_name": "Acme Realty", "email": None}])).extract(page)
    assert rows[0]["full_name"] == "Jane Smith"
    assert rows[0]["agency_name"] == "Acme Realty"  # untouched


def test_jsonld_worksfor_feeds_agency_column():
    html = """<html><head><script type="application/ld+json">
    {"@type": "RealEstateAgent", "name": "Jane Broker",
     "telephone": "412-555-0100",
     "worksFor": {"@type": "Organization", "name": "Howard Hanna"}}
    </script></head><body><h1>Agent</h1></body></html>"""
    ld = extract_jsonld_fields(html)
    assert ld.get("org") == "Howard Hanna"
    cols = [("Full Name", "full_name"), ("Agency", "agency"),
            ("Phone", "phone"), ("Source URL", "source_url")]
    rows = FreeFormExtractor(cols, entity="real estate agent", client=FakeLLM(
        [{"full_name": "Jane Broker", "agency": None, "phone": None}])).extract(html)
    assert rows[0]["agency"] == "Howard Hanna"


def test_url_only_requested_fields_fold_into_source_url():
    cols = derive_columns(["Full Name", "URL"])
    assert [k for _, k in cols] == ["full_name", "source_url"]
    cols = derive_columns(["Full Name", "Source Link"])
    assert [k for _, k in cols] == ["full_name", "source_url"]


def test_specific_url_fields_are_kept_as_columns():
    cols = derive_columns(["Profile URL", "Website"])
    keys = [k for _, k in cols]
    assert "profile_url" in keys and "website" in keys


def test_obfuscated_email_normalized_and_grounded():
    page = "<html><body><div>Jane Smith — jane.smith [at] realco.com</div></body></html>"
    rows = _extract([{"full_name": "Jane Smith", "first_name": None,
                      "last_name": None, "email": "jane.smith [at] realco.com"}], page)
    assert rows[0]["email"] == "jane.smith@realco.com"
