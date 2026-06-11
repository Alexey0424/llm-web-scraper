"""robots.txt fetching and evaluation.

CURRENTLY UNUSED: since the 2026-06-04 "Direct Mode" change, ``compliance.gate_url``
no longer calls this module, so robots.txt is not enforced anywhere. The module
is kept working so the gate can be wired back in (see LEGALITY_OF_THE_CRAWL.md
§2). When wired in: if a path is disallowed for our user-agent, we do not fetch
it; network failures fetching robots.txt are treated as "no robots file".
"""

from __future__ import annotations

import urllib.robotparser
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import httpx


@lru_cache(maxsize=256)
def _parser_for(base: str, user_agent: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(base, "/robots.txt")
    rp.set_url(robots_url)
    try:
        resp = httpx.get(robots_url, timeout=15, headers={"User-Agent": user_agent})
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            # No robots.txt (404) conventionally means "allow all".
            rp.parse([])
    except Exception:
        # Treat as "no robots file available"; caller may choose to be cautious.
        rp.parse([])
    return rp


def is_allowed(url: str, user_agent: str) -> bool:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    rp = _parser_for(base, user_agent)
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True
