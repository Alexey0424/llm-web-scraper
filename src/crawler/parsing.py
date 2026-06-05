"""HTML parsing helpers — Scrapling ``Selector`` primary, BeautifulSoup fallback.

Scrapling is this project's primary parser (adaptive, lexbor-backed). The crawler
reads HTML through these helpers so the parsing goes through Scrapling whenever it
is importable, and degrades to BeautifulSoup only when it is not (e.g. host test
runs without Scrapling installed). Callers get plain Python data and never need to
know which engine ran.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def selector(html: str):
    """Return a Scrapling ``Selector`` for ``html``, or ``None`` if unavailable."""
    try:
        from scrapling import Selector
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        return Selector(html)
    except Exception as exc:  # noqa: BLE001
        log.debug("Scrapling Selector init failed (%s)", exc)
        return None


def extract_anchors(html: str) -> list[tuple[str, str]]:
    """Return ``(href, anchor_text)`` for every ``<a href>``.

    Scrapling ``Selector.css('a')`` first; BeautifulSoup fallback.
    """
    sel = selector(html)
    if sel is not None:
        try:
            out: list[tuple[str, str]] = []
            for a in sel.css("a"):
                try:
                    href = (a.attrib.get("href") or "").strip()
                except Exception:  # noqa: BLE001
                    href = ""
                if href:
                    out.append((href, (a.text or "").strip()))
            return out
        except Exception as exc:  # noqa: BLE001
            log.debug("Scrapling anchor parse failed (%s); using bs4", exc)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    return [((a.get("href") or "").strip(), a.get_text(" ", strip=True))
            for a in soup.find_all("a", href=True)]


def text_and_contacts(html: str, limit: int = 6000) -> tuple[str, int, int]:
    """Return ``(visible_text[:limit], mailto_count, tel_count)``.

    Scrapling ``get_all_text()`` already excludes script/style; contact links are
    counted with CSS selectors. BeautifulSoup fallback strips script/style itself.
    """
    sel = selector(html)
    if sel is not None:
        try:
            mailtos = len(sel.css('a[href^="mailto:"]'))
            tels = len(sel.css('a[href^="tel:"]'))
            text = sel.get_all_text() or ""
            return text[:limit], mailtos, tels
        except Exception as exc:  # noqa: BLE001
            log.debug("Scrapling text parse failed (%s); using bs4", exc)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    mailtos = len(soup.select('a[href^="mailto:"]'))
    tels = len(soup.select('a[href^="tel:"]'))
    return text[:limit], mailtos, tels
