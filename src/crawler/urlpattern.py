"""Induce repeating URL clusters from a page's links (anchor-text-free).

Detail/record pages on a listing share a path prefix whose final segment varies
(``/profile/<name>``, ``/dp/<id>``, ``/article/<slug>``). We group same-site URLs
by "prefix = path minus last segment" and rank clusters by how many DISTINCT
leaves they have -- the record cluster is typically the largest. This is fully
deterministic and ignores anchor text, so a link whose visible text is just a
person's name is still grouped correctly. The caller (optionally a single LLM
call) decides which cluster is the target record type.
"""
from __future__ import annotations

from urllib.parse import urlparse


def _prefix_leaf(url: str) -> tuple[str, str] | None:
    segs = [s for s in urlparse(url).path.split("/") if s]
    if not segs:
        return None
    return "/" + "/".join(segs[:-1]), segs[-1]


def induce_clusters(urls: list[str], min_size: int = 3) -> list[dict]:
    """Group URLs by path-prefix; return clusters (size>=min_size) largest-first.

    Each cluster: ``{"prefix": "/profile", "size": <distinct leaves>, "samples": [...]}``.
    """
    groups: dict[str, set[str]] = {}
    for u in urls:
        pl = _prefix_leaf(u)
        if not pl:
            continue
        prefix, leaf = pl
        groups.setdefault(prefix, set()).add(leaf)
    clusters = [
        {"prefix": p, "size": len(leaves), "samples": sorted(leaves)[:5]}
        for p, leaves in groups.items()
        if len(leaves) >= min_size
    ]
    clusters.sort(key=lambda c: -c["size"])
    return clusters


def matches_pattern(url: str, prefix: str) -> bool:
    """True if ``url`` is a single-leaf child of ``prefix`` (a detail page of it)."""
    if not prefix:
        return False
    pl = _prefix_leaf(url)
    return bool(pl and pl[0] == prefix)
