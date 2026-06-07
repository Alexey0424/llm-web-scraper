"""Offline tests for batch combined-Excel writers (stacked + multisheet)."""

import openpyxl

from src.crawler.fields import derive_columns
from src.exporters import dynamic_exporter


def test_write_xlsx_stacked_adds_site_column_and_stacks_rows(tmp_path):
    cols = derive_columns(["Full Name", "Email"])  # + source_url
    sections = [
        ("https://a.com", [
            {"full_name": "Ann A", "email": "ann@a.com", "source_url": "https://a.com/p1"},
        ]),
        ("https://b.com", [
            {"full_name": "Bob B", "email": "bob@b.com", "source_url": "https://b.com/p1"},
            {"full_name": "Cy C", "email": None, "source_url": "https://b.com/p2"},
        ]),
    ]
    out = dynamic_exporter.write_xlsx_stacked(cols, sections, tmp_path / "stacked.xlsx")
    ws = openpyxl.load_workbook(out).active
    header = [c.value for c in ws[1]]
    # Site is prepended; the prompt columns follow.
    assert header == ["Site", "Full Name", "Email", "Source URL"]
    # 1 header + 3 data rows
    assert ws.max_row == 4
    assert ws.cell(row=2, column=1).value == "https://a.com"
    assert ws.cell(row=2, column=2).value == "Ann A"
    assert ws.cell(row=3, column=1).value == "https://b.com"
    assert ws.cell(row=4, column=1).value == "https://b.com"
    assert ws.cell(row=4, column=3).value is None  # missing email blank


def test_write_xlsx_multisheet_one_sheet_per_op(tmp_path):
    sheets = [
        ("Agents NJ", derive_columns(["Name", "Email"]),
         [{"name": "Ann", "email": "ann@x.com", "source_url": "u1"}]),
        ("Listings", derive_columns(["Address", "Price"]),
         [{"address": "1 Main", "price": "100", "source_url": "u2"}]),
    ]
    out = dynamic_exporter.write_xlsx_multisheet(sheets, tmp_path / "multi.xlsx")
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Agents NJ", "Listings"]
    a = wb["Agents NJ"]
    assert [c.value for c in a[1]] == ["Name", "Email", "Source URL"]
    assert a.cell(row=2, column=1).value == "Ann"
    b = wb["Listings"]
    assert [c.value for c in b[1]] == ["Address", "Price", "Source URL"]


def test_multisheet_sanitizes_and_dedups_titles(tmp_path):
    long_title = "https://really-long-domain-example.com/agents/list"  # >31 chars + slashes
    sheets = [
        (long_title, derive_columns(["Name"]), [{"name": "A", "source_url": "u"}]),
        (long_title, derive_columns(["Name"]), [{"name": "B", "source_url": "u"}]),
    ]
    out = dynamic_exporter.write_xlsx_multisheet(sheets, tmp_path / "m.xlsx")
    wb = openpyxl.load_workbook(out)
    names = wb.sheetnames
    assert len(names) == 2
    for n in names:
        assert len(n) <= 31
        assert not (set(n) & set('[]:*?/\\'))
    assert names[0] != names[1]  # de-duplicated
