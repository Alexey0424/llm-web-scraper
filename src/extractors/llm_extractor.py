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


# Elements whose end marks a visual line/record boundary. Table cells (td/th)
# deliberately stay inline so one table row reads as one line.
_BLOCK_TAGS = (
    "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "dt", "dd", "blockquote",
    "figure", "ul", "ol", "table",
)


def html_to_signal_text(html: str, *, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    """Reduce HTML to line-structured text, inlining mailto/tel as markers.

    Uses BeautifulSoup specifically because it *mutates* the DOM in document order
    (``a.append(...)``) to drop ``[email: ...]`` / ``[phone: ...]`` markers right
    next to each record, which materially improves the LLM's name→contact
    association. Scrapling's read-oriented ``Selector`` doesn't do in-place
    mutation cleanly, so bs4 is the right tool here.

    Block-level elements become line breaks (instead of flattening the whole
    page to one line) so each record card/row reads as its own group of lines —
    the visual grouping is what tells the LLM which phone belongs to which name.
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
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    lines = (re.sub(r"\s+", " ", ln).strip()
             for ln in soup.get_text(" ").split("\n"))
    text = "\n".join(ln for ln in lines if ln)
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
