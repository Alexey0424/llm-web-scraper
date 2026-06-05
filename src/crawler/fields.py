"""Turn the prompt's requested fields into ordered export columns.

Free-form mode: the user's prompt decides the columns (FetchFox-style). The
Planner extracts a list of human field names into the crawl intent; this module
normalizes them into ordered ``(header, key)`` pairs where ``header`` is what the
user sees in the Excel and ``key`` is a stable snake_case identifier used in the
extraction JSON and the record dicts. A ``Source URL`` provenance column is always
appended.
"""

from __future__ import annotations

import re

SOURCE_URL = ("Source URL", "source_url")


def _key(header: str) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    return k or "field"


def _titlecase(header: str) -> str:
    h = re.sub(r"\s+", " ", header.strip())
    # Leave already-Capitalized / ALLCAPS-ish headers alone; otherwise title-case.
    if h and h == h.lower():
        return h.title()
    return h


def derive_columns(field_names: list[str]) -> list[tuple[str, str]]:
    """Return ordered, de-duplicated (header, key) pairs + a Source URL column.

    Falls back to a sensible agent default if the prompt yielded no fields.
    """
    cleaned = [f for f in (field_names or []) if f and str(f).strip()]
    if not cleaned:
        cleaned = ["Full Name", "Email", "Phone", "Agency", "City", "State"]
    cols: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in cleaned:
        header = _titlecase(str(name))
        key = _key(header)
        if key in seen or key == SOURCE_URL[1]:
            continue
        seen.add(key)
        cols.append((header, key))
    cols.append(SOURCE_URL)
    return cols


def dedup_key(columns: list[tuple[str, str]]) -> str:
    """Pick the record field used to de-duplicate rows across pages.

    Prefer an email-like column, then a name-like one, else the first column.
    """
    keys = [k for _, k in columns if k != SOURCE_URL[1]]
    for k in keys:
        if "email" in k:
            return k
    for k in keys:
        if "name" in k:
            return k
    return keys[0] if keys else SOURCE_URL[1]
