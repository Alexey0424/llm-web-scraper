"""Fetch gate — currently "Direct Mode" (robots.txt NOT checked).

Originally this module enforced a strict boundary (every fetch checked against
robots.txt via ``utils.robots``). The project owner switched it to Direct Mode
in the 2026-06-04 "Vanilla baseline": ``gate_url`` only validates the URL scheme
and allows everything else, so any pasted URL is fetched. ``utils/robots.py``
still works and can be wired back in here to restore robots enforcement (see
LEGALITY_OF_THE_CRAWL.md §2).

What still holds elsewhere in the fetch path: per-domain rate limiting, bounded
retries, same-domain scope, and block/CAPTCHA pages being detected and reported
(``sources.base.detect_blocked_page``) rather than solved.
"""

# compliance.py
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class GateResult:
    allowed: bool
    reason: str

def gate_source(source_cfg: dict = None) -> GateResult:
    """Always allow the source."""
    return GateResult(True, "Direct Mode: All sources allowed.")

def gate_url(url: str, user_agent: str) -> GateResult:
    """Scheme check only — robots.txt is deliberately skipped (Direct Mode),
    so data can be fetched from any URL the user pastes."""
    if not url.startswith(("http://", "https://")):
        return GateResult(False, f"Invalid URL scheme: {url}")

    return GateResult(True, "Direct Mode: Bypassing robots.txt")

