"""Export free-form records to Excel / CSV / JSONL with prompt-derived columns.

Columns are ``(header, key)`` pairs decided per crawl; rows are dicts keyed by
``key``. The header row is what the user sees; values are looked up by key. This
is the free-form counterpart to the fixed 9-column ``excel_exporter``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Font


def _rows_as_lists(columns: list[tuple[str, str]], rows: Iterable[dict]) -> list[list]:
    keys = [k for _, k in columns]
    return [[r.get(k) for k in keys] for r in rows]


def write_xlsx(columns: list[tuple[str, str]], rows: Iterable[dict], path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    headers = [h for h, _ in columns]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for values in _rows_as_lists(columns, rows):
        ws.append(values)
    for i, (h, k) in enumerate(columns, start=1):
        width = 46 if k == "source_url" else (34 if "email" in k or "agency" in k else 20)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    wb.save(path)
    return str(path)


def write_csv(columns: list[tuple[str, str]], rows: Iterable[dict], path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _ in columns])
        for values in _rows_as_lists(columns, rows):
            w.writerow(["" if v is None else v for v in values])
    return str(path)


def write_jsonl(columns: list[tuple[str, str]], rows: Iterable[dict], path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = [k for _, k in columns]
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({k: r.get(k) for k in keys}, ensure_ascii=False) + "\n")
    return str(path)
