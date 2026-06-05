"""Offline tests for free-form mode: prompt-derived columns + dynamic export."""

import openpyxl

from src.crawler.fields import derive_columns, dedup_key
from src.exporters import dynamic_exporter


def test_derive_columns_from_prompt_fields():
    cols = derive_columns(["Full Name", "Email", "Brokerage", "Years Active"])
    headers = [h for h, _ in cols]
    keys = [k for _, k in cols]
    assert headers == ["Full Name", "Email", "Brokerage", "Years Active", "Source URL"]
    assert keys == ["full_name", "email", "brokerage", "years_active", "source_url"]


def test_derive_columns_dedups_and_defaults():
    # duplicates collapse; source_url always appended exactly once
    cols = derive_columns(["Email", "email", "EMAIL"])
    assert [k for _, k in cols] == ["email", "source_url"]
    # empty -> sensible agent default
    cols = derive_columns([])
    assert ("Source URL", "source_url") in cols
    assert len(cols) > 1


def test_dedup_key_prefers_email_then_name():
    assert dedup_key(derive_columns(["Name", "Email", "City"])) == "email"
    assert dedup_key(derive_columns(["Full Name", "City"])) == "full_name"
    assert dedup_key(derive_columns(["Color", "Size"])) == "color"


def test_dynamic_exporter_roundtrip(tmp_path):
    cols = derive_columns(["Full Name", "Email", "City"])
    rows = [
        {"full_name": "Maria Gonzalez", "email": "maria@x.com", "city": "Newark",
         "source_url": "http://x/agents"},
        {"full_name": "David Chen", "email": None, "city": "Edison",
         "source_url": "http://x/agents"},
    ]
    xlsx = dynamic_exporter.write_xlsx(cols, rows, tmp_path / "out.xlsx")
    ws = openpyxl.load_workbook(xlsx).active
    header = [c.value for c in ws[1]]
    assert header == ["Full Name", "Email", "City", "Source URL"]
    assert ws.cell(row=2, column=1).value == "Maria Gonzalez"
    assert ws.cell(row=2, column=4).value == "http://x/agents"
    assert ws.cell(row=3, column=2).value is None  # missing email stays blank

    csv_path = dynamic_exporter.write_csv(cols, rows, tmp_path / "out.csv")
    text = open(csv_path, encoding="utf-8").read()
    assert "Full Name,Email,City,Source URL" in text
    assert "Maria Gonzalez" in text
