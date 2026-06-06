"""Pull structured fields from ``<script type="application/ld+json">`` blocks.

Many detail pages (people, products, local businesses, real-estate agents) embed
schema.org JSON-LD with exactly the fields a user wants -- name, telephone,
email, address -- which is far more reliable than scraping rendered text (and
often present even when the visible page hides contact info behind a button).
Returns a flat dict of the high-value keys. Tolerant of arrays, ``@graph``
wrappers, nested objects, and malformed blocks (skipped).
"""
from __future__ import annotations

import json
import re
import urllib.parse

_LD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_SCALAR_KEYS = ("name", "telephone", "email", "url")


def _walk(obj, out: dict) -> None:
    if isinstance(obj, dict):
        for k in _SCALAR_KEYS:
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip() and k not in out:
                out[k] = str(v).strip()
        addr = obj.get("address")
        if isinstance(addr, dict):
            if addr.get("addressLocality") and "city" not in out:
                out["city"] = str(addr["addressLocality"]).strip()
            if addr.get("addressRegion") and "state" not in out:
                out["state"] = str(addr["addressRegion"]).strip()
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def extract_jsonld_fields(html: str) -> dict:
    """Return {name, telephone, email, url, city, state} found in JSON-LD (any subset)."""
    out: dict = {}
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        _walk(data, out)
    return out


_MAILTO = re.compile(r'mailto:([^"\'?\s<>]+)', re.IGNORECASE)
_TEL = re.compile(r'href=["\']tel:([+0-9().\-\s]{7,})["\']', re.IGNORECASE)


def extract_contact_fields(html: str) -> dict:
    """JSON-LD fields, augmented with ``mailto:`` / ``tel:`` scraped from raw HTML.

    JSON-LD frequently carries ``telephone`` but not ``email`` (the email is in a
    ``mailto:`` link), so this fallback fills the gaps that the LLM text pass
    often misses on detail pages.
    """
    out = extract_jsonld_fields(html)
    if "email" not in out:
        m = _MAILTO.search(html or "")
        if m:
            out["email"] = urllib.parse.unquote(m.group(1).split("?")[0]).strip()
    if "telephone" not in out:
        m = _TEL.search(html or "")
        if m:
            out["telephone"] = m.group(1).strip()
    return out
