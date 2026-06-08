"""Offline tests for deterministic record dedup + the export-file ledger."""

import openpyxl

from src.crawler import dedup
from src.exporters import dynamic_exporter
from src.crawler.fields import derive_columns


def test_record_key_prefers_email_then_phone_then_name():
    assert dedup.record_key({"email": "Ann@X.com"}) == "e:ann@x.com"
    # email beats phone
    assert dedup.record_key({"email": "a@x.com", "phone": "201-555-1212"}) == "e:a@x.com"
    # phone when no email; normalized to last 10 digits
    assert dedup.record_key({"phone 1": "+1 (201) 555-1212"}) == "p:2015551212"
    # name (+ city) when neither
    assert dedup.record_key({"full_name": "María  Gómez", "city": "Newark"}) == "n:mara gmez|newark"
    # nothing usable -> None (never dedupable)
    assert dedup.record_key({"agency": "Acme"}) is None
    assert dedup.record_key({"email": None, "name": ""}) is None


def test_record_key_works_on_human_headers():
    # rows read back from an .xlsx use human headers as keys
    assert dedup.record_key({"Email": "B@Y.COM"}) == "e:b@y.com"
    assert dedup.record_key({"Full Name": "Bob Lee", "State": "NJ"}) == "n:bob lee|nj"


def test_dedupe_sections_drops_cross_section_duplicates():
    sections = [
        ("https://a.com", [
            {"name": "Ann", "email": "ann@x.com"},
            {"name": "Bob", "email": "bob@x.com"},
        ]),
        ("https://b.com", [
            {"name": "Ann A", "email": "ANN@x.com"},   # dup of Ann by email
            {"name": "Cy", "email": "cy@x.com"},
            {"name": "NoId"},                            # no identifier -> kept
        ]),
    ]
    out, removed = dedup.dedupe_sections(sections)
    assert removed == 1
    kept = [r for _, rows in out for r in rows]
    assert len(kept) == 4
    emails = [r.get("email") for _, rows in out for r in rows]
    assert "ANN@x.com" not in emails  # the later duplicate was dropped


def test_build_export_ledger_indexes_xlsx_and_caches(tmp_path):
    cols = derive_columns(["Full Name", "Email"])
    dynamic_exporter.write_xlsx(cols, [
        {"full_name": "Ann", "email": "ann@x.com", "source_url": "u"},
    ], tmp_path / "one.xlsx")
    dynamic_exporter.write_xlsx(cols, [
        {"full_name": "Ann2", "email": "ann@x.com", "source_url": "u"},   # same email, other file
        {"full_name": "Bob", "email": "bob@x.com", "source_url": "u"},
    ], tmp_path / "two.xlsx")

    dedup.clear_ledger_cache()
    led = dedup.build_export_ledger(tmp_path)
    assert sorted(led["e:ann@x.com"]) == ["one.xlsx", "two.xlsx"]
    assert led["e:bob@x.com"] == ["two.xlsx"]

    # deleting a file is reflected (cache invalidates on the dir's file set)
    (tmp_path / "one.xlsx").unlink()
    led2 = dedup.build_export_ledger(tmp_path)
    assert led2["e:ann@x.com"] == ["two.xlsx"]


def test_iter_xlsx_rows_roundtrip(tmp_path):
    cols = derive_columns(["Name", "Email"])
    dynamic_exporter.write_xlsx(cols, [{"name": "Ann", "email": "a@x.com", "source_url": "u"}],
                                tmp_path / "r.xlsx")
    rows = list(dedup.iter_xlsx_rows(tmp_path / "r.xlsx"))
    assert rows[0]["Name"] == "Ann" and rows[0]["Email"] == "a@x.com"
