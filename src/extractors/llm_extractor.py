"""Shared HTML→text helpers for LLM extraction.

These utilities prepare fetched HTML for a local LLM: reduce it to compact text
that preserves the high-signal bits (``mailto:``/``tel:`` inlined as markers), and
split long text into overlapping chunks. They are consumed by the free-form
crawler extractor (``src/crawler/freeform.py``).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Sized so the free-form extractor's chunking (4 chunks x 6000 chars, 400
# overlap) is fed rather than starved: a 12k cap silently dropped the back
# half of large rosters before the LLM ever saw them.
DEFAULT_TEXT_LIMIT = 24000


def html_to_signal_text(html: str, *, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    """Reduce HTML to text, inlining mailto/tel hrefs so the LLM can see them.

    Uses BeautifulSoup specifically because it *mutates* the DOM in document order
    (``a.append(...)``) to drop ``[email: ...]`` / ``[phone: ...]`` markers right
    next to each record, which materially improves the LLM's name→contact
    association. Scrapling's read-oriented ``Selector`` doesn't do in-place
    mutation cleanly, so bs4 is the right tool here.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            a.append(f" [email: {unquote(href[7:].split('?')[0])}]")
        elif href.lower().startswith("tel:"):
            a.append(f" [phone: {href[4:]}]")
    text = re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))
    if len(text) > limit:
        log.info("html_to_signal_text: page text truncated %d -> %d chars",
                 len(text), limit)
    return text[:limit]


def _chunks(text: str, size: int = 6000, overlap: int = 400) -> list[str]:
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i : i + size])
        i += size - overlap
    return out


def _clean(v) -> str | None:
    """Normalize a scalar field: collapse whitespace, treat null-ish as None."""
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown", ""}:
        return None
    return s
