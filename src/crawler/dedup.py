"""Deterministic record deduplication + an export-file ledger.

Methodology (entity-resolution best practice for data that carries reliable
identifiers): normalize -> build a canonical key from the strongest identifier
available -> match on equality. Email and phone are trustworthy keys, so we use
deterministic matching; a name(+location) key is the weak last resort, and a
record with no usable identifier is never treated as a duplicate (we keep it).
Fuzzy/probabilistic matching (Jaro-Winkler, Splink, ...) is intentionally out of
scope — it's only needed when no shared identifier exists.

Two consumers:
- ``dedupe_sections`` — cross-site dedup when stacking a bulk batch into one sheet.
- ``build_export_ledger`` — index existing export files so the UI can flag a
  record that was already saved (exported) before. The export files on disk are
  the single source of truth, so deleting a file removes its rows from the ledger.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_KEYS = ("phone", "tel", "cell", "mobile")
_LOC_KEYS = ("city", "state", "location", "town", "region", "province")


def _norm_email(v) -> str | None:
    if v is None:
        return None
    m = _EMAIL_RE.search(str(v))
    return m.group(0).lower() if m else None


def _norm_phone(v) -> str | None:
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else None


def _norm_text(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[^a-z0-9 ]", "", str(v).lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def record_key(row: dict) -> str | None:
    """Canonical identity for a record dict, or None when nothing reliable exists.

    Works on both internal rows (snake_case keys) and rows read back from an
    .xlsx (human header keys) — matching is by lowercased-key substring.
    """
    items = [(str(k).lower(), v) for k, v in row.items()
             if v is not None and str(v).strip() != ""]

    for kl, v in items:                       # 1) email — strongest
        if "email" in kl:
            e = _norm_email(v)
            if e:
                return "e:" + e

    for kl, v in items:                       # 2) phone
        if any(t in kl for t in _PHONE_KEYS):
            p = _norm_phone(v)
            if p:
                return "p:" + p

    name = None                               # 3) name (+ location), weak
    for kl, v in items:
        if "name" in kl and "first" not in kl and "last" not in kl:
            name = _norm_text(v)
            if name:
                break
    if not name:
        first = last = None
        for kl, v in items:
            if "name" in kl and "first" in kl:
                first = _norm_text(v)
            elif "name" in kl and "last" in kl:
                last = _norm_text(v)
        if first or last:
            name = " ".join(x for x in (first, last) if x)
    if name:
        loc = None
        for kl, v in items:
            if kl.strip() in _LOC_KEYS:
                t = _norm_text(v)
                if t:
                    loc = (loc + "|" + t) if loc else t
        return "n:" + name + ("|" + loc if loc else "")

    return None


def dedupe_sections(sections: list[tuple[str, Iterable[dict]]]):
    """Cross-section dedup for a stacked bulk batch (first occurrence wins).

    ``sections`` is ``[(label, rows), ...]``. Returns ``(new_sections, removed)``.
    Rows without a usable key are always kept.
    """
    seen: set[str] = set()
    removed = 0
    out: list[tuple[str, list[dict]]] = []
    for label, rows in sections:
        kept: list[dict] = []
        for r in rows:
            k = record_key(r)
            if k is not None:
                if k in seen:
                    removed += 1
                    continue
                seen.add(k)
            kept.append(r)
        out.append((label, kept))
    return out, removed


def iter_xlsx_rows(path: str | Path) -> Iterator[dict]:
    """Yield each data row of an .xlsx as a dict keyed by its column header."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        if ws is None:
            return
        it = ws.iter_rows(values_only=True)
        try:
            headers = next(it)
        except StopIteration:
            return
        headers = [str(h) if h is not None else "" for h in headers]
        for vals in it:
            yield {headers[i]: vals[i] for i in range(min(len(headers), len(vals)))}
    finally:
        wb.close()


# Cache the ledger keyed on the exports dir's file set + mtimes/sizes, so a page
# load doesn't re-read every workbook, yet adding/removing/changing a file rebuilds.
_ledger_cache: dict = {"sig": None, "ledger": {}}


def clear_ledger_cache() -> None:
    _ledger_cache["sig"] = None
    _ledger_cache["ledger"] = {}


def build_export_ledger(exports_dir: str | Path, only_ext=(".xlsx",)) -> dict[str, list[str]]:
    """Index export files into ``{record_key: [filenames]}``.

    Scans only .xlsx by default (each export writes xlsx/csv/jsonl with identical
    data, so one format is enough and avoids triple-counting).
    """
    exports_dir = Path(exports_dir)
    if not exports_dir.exists():
        return {}
    files = sorted(p for p in exports_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in only_ext)
    try:
        sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    except OSError:
        sig = None
    if sig is not None and _ledger_cache["sig"] == sig:
        return _ledger_cache["ledger"]

    ledger: dict[str, list[str]] = {}
    for p in files:
        try:
            seen_here: set[str] = set()
            for row in iter_xlsx_rows(p):
                k = record_key(row)
                if not k or k in seen_here:
                    continue
                seen_here.add(k)
                ledger.setdefault(k, []).append(p.name)
        except Exception:  # noqa: BLE001 - a locked/corrupt file shouldn't break the page
            continue

    _ledger_cache["sig"] = sig
    _ledger_cache["ledger"] = ledger
    return ledger
