"""Read a site's /sitemap.xml to seed the crawl frontier.

Many sites enumerate every record/detail page in their sitemap, which solves
the whole discovery problem in one fetch instead of link-hopping toward it.
Fetching goes through the project's shared fetcher, so the per-domain rate
limit and block detection apply exactly as for any page. Bounded on both the
number of sitemap documents (index recursion) and the number of URLs returned;
a missing or malformed sitemap simply yields nothing.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from .frontier import same_site

log = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)


def fetch_sitemap_urls(seed_url: str, fetcher, *, max_urls: int = 500,
                       max_docs: int = 5) -> list[str]:
    """Return same-site page URLs from the seed domain's sitemap (or [])."""
    p = urlparse(seed_url)
    if not p.scheme or not p.netloc:
        return []
    queue = [f"{p.scheme}://{p.netloc}/sitemap.xml"]
    seen_docs: set[str] = set()
    out: list[str] = []
    seen_urls: set[str] = set()
    while queue and len(seen_docs) < max_docs and len(out) < max_urls:
        doc = queue.pop(0)
        if doc in seen_docs:
            continue
        seen_docs.add(doc)
        res = fetcher.get(doc)
        if not res.ok:
            log.debug("sitemap fetch failed %s: %s", doc, res.reason[:80])
            continue
        locs = [u for u in _LOC_RE.findall(res.content)
                if u.startswith(("http://", "https://")) and same_site(seed_url, u)]
        if re.search(r"<sitemapindex", res.content, re.IGNORECASE):
            queue.extend(u for u in locs if not u.endswith(".gz"))
            continue
        for u in locs:
            if u not in seen_urls:
                seen_urls.add(u)
                out.append(u)
                if len(out) >= max_urls:
                    break
    if out:
        log.info("sitemap: %d URL(s) from %d document(s)", len(out), len(seen_docs))
    return out
