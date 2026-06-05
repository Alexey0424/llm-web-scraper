"""Deterministic IDs and content hashing."""

from __future__ import annotations

import hashlib
import re


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


def deterministic_id(state: str | None, email: str | None, full_name: str | None) -> str:
    """Stable identity key for an agent.

    Email is the strongest signal (the template guarantees one per row). When an
    email is missing we fall back to state+name so records still dedupe sensibly.
    """
    email_n = _norm(email)
    key = email_n if email_n else f"{_norm(state)}|{_norm(full_name)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def content_hash(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha1(content).hexdigest()
