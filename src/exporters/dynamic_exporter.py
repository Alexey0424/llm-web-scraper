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


def _set_widths(ws, columns: list[tuple[str, str]], offset: int = 0) -> None:
    for i, (h, k) in enumerate(columns, start=1 + offset):
        width = 46 if k == "source_url" else (34 if "email" in k or "agency" in k else 20)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width


def write_xlsx_stacked(columns: list[tuple[str, str]],
                       sections: list[tuple[str, Iterable[dict]]],
                       path: str | Path) -> str:
    """One sheet stacking rows from several runs that share the same columns.

    ``sections`` is ``[(site_label, rows), ...]``. A leading ``Site`` column
    records which seed each row came from (the bulk/shared-prompt mode, where
    every URL is queried with the same prompt so the columns line up).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Combined"
    headers = ["Site"] + [h for h, _ in columns]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    keys = [k for _, k in columns]
    for site_label, rows in sections:
        for r in rows:
            ws.append([site_label] + [r.get(k) for k in keys])
    ws.column_dimensions["A"].width = 30
    _set_widths(ws, columns, offset=1)
    wb.save(path)
    return str(path)


def _sanitize_sheet_title(title: str, used: set[str]) -> str:
    """Make an Excel-legal, unique sheet title (≤31 chars, no []:*?/\\)."""
    clean = "".join(c for c in (title or "Sheet") if c not in '[]:*?/\\').strip()
    clean = (clean or "Sheet")[:31]
    base = clean
    n = 2
    while clean.lower() in used:
        suffix = f" ({n})"
        clean = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


def write_xlsx_multisheet(sheets: list[tuple[str, list[tuple[str, str]], Iterable[dict]]],
                          path: str | Path) -> str:
    """One workbook, one sheet per operation (the per-URL-prompt queue mode).

    ``sheets`` is ``[(title, columns, rows), ...]``. Each op keeps its own
    column set on its own tab; titles are sanitized + de-duplicated for Excel.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)  # drop the default sheet; we add our own
    used: set[str] = set()
    for title, columns, rows in sheets:
        ws = wb.create_sheet(_sanitize_sheet_title(title, used))
        headers = [h for h, _ in columns]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for values in _rows_as_lists(columns, rows):
            ws.append(values)
        _set_widths(ws, columns)
    if not wb.sheetnames:  # never leave an empty workbook
        wb.create_sheet("Empty")
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
