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

# Node categories, used to keep page furniture (breadcrumbs: name="New Jersey")
# and sitewide publisher/footer orgs from masquerading as the page's record.
# NOTE: real sites mark individual people up as LocalBusiness (e.g. Zillow agent
# profiles), so LocalBusiness deliberately stays in the neutral "other" bucket.
_PERSON_TYPES = {"person", "realestateagent"}
_SITE_TYPES = {
    "breadcrumblist", "listitem", "website", "webpage", "searchaction",
    "sitenavigationelement", "itemlist", "collectionpage", "searchresultspage",
    "imageobject", "aboutpage", "contactpage", "faqpage",
}
# Lower number = preferred source for the page's record fields.
_CATEGORY_PRIORITY = {"person": 0, "other": 1, "org": 2, "site": 3}


def _categorize(obj: dict) -> str | None:
    """Category of a typed node ('person'|'org'|'site'|'other'), None if untyped."""
    t = obj.get("@type")
    if not t:
        return None
    types = t if isinstance(t, list) else [t]
    norm = {str(x).strip().lower() for x in types if x}
    if norm & _PERSON_TYPES:
        return "person"
    if norm & _SITE_TYPES:
        return "site"
    if "corporation" in norm or any("organization" in x for x in norm):
        return "org"
    return "other"


def _nodes(obj, inherited: str = "other"):
    """Yield (category, dict_node) depth-first; untyped nodes inherit their
    nearest typed ancestor's category (so a Person's nested parts stay person)."""
    if isinstance(obj, dict):
        cat = _categorize(obj) or inherited
        yield cat, obj
        for v in obj.values():
            yield from _nodes(v, cat)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nodes(v, inherited)


def extract_jsonld_fields(html: str) -> dict:
    """Return {name, telephone, email, url, city, state, org} found in JSON-LD (any subset).

    Person-like nodes are preferred over orgs, and page furniture (breadcrumbs,
    WebSite, ...) is the last resort — so a roster page's BreadcrumbList no
    longer wins the "name" slot from a real record. ``_name_type`` records which
    category supplied the name, for callers that gate on it.
    """
    nodes: list[tuple[str, dict]] = []
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        nodes.extend(_nodes(data))
    # Stable sort: category priority first, document order within a category.
    nodes.sort(key=lambda cn: _CATEGORY_PRIORITY[cn[0]])
    out: dict = {}
    for cat, obj in nodes:
        for k in _SCALAR_KEYS:
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip() and k not in out:
                out[k] = str(v).strip()
                if k == "name":
                    out["_name_type"] = cat
        wf = obj.get("worksFor")
        if "org" not in out:
            cand = (wf if isinstance(wf, dict)
                    else wf[0] if isinstance(wf, list) and wf and isinstance(wf[0], dict)
                    else None)
            if cand and cand.get("name"):
                out["org"] = str(cand["name"]).strip()
        addr = obj.get("address")
        if isinstance(addr, dict):
            if addr.get("addressLocality") and "city" not in out:
                out["city"] = str(addr["addressLocality"]).strip()
            if addr.get("addressRegion") and "state" not in out:
                out["state"] = str(addr["addressRegion"]).strip()
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
