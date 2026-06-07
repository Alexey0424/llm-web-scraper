"""Batch crawl planning — turn parsed form input into an ordered job list.

Pure and I/O-free so it can be unit-tested without Flask. Two batch modes:

- ``bulk``  — many URLs, one shared prompt -> jobs share columns -> combine into
  a single stacked sheet (``combine="stack"``).
- ``queue`` — ordered (URL, prompt) pairs run serially -> each op keeps its own
  columns -> combine into one workbook, one sheet per op (``combine="sheets"``).
"""

from __future__ import annotations


def build_jobs(mode: str, *, urls=None, prompt=None, pairs=None):
    """Return ``(jobs, combine)`` where jobs is ``[{"url","prompt"}, ...]``.

    Raises ``ValueError`` with a user-facing message when the input is empty or
    the mode is unknown.
    """
    if mode == "bulk":
        clean_urls = [u.strip() for u in (urls or []) if u and u.strip()]
        shared = (prompt or "").strip()
        if not clean_urls:
            raise ValueError("Add at least one URL.")
        if not shared:
            raise ValueError("A prompt is required for the bulk run.")
        jobs = [{"url": u, "prompt": shared} for u in clean_urls]
        return jobs, "stack"

    if mode == "queue":
        jobs = []
        for p in (pairs or []):
            url = (p.get("url") or "").strip()
            pr = (p.get("prompt") or "").strip()
            if url and pr:
                jobs.append({"url": url, "prompt": pr})
        if not jobs:
            raise ValueError("Add at least one operation with both a URL and a prompt.")
        return jobs, "sheets"

    raise ValueError(f"Unknown batch mode: {mode!r}")
