"""Phone and email normalization.

Phone target format matches the template: ``NNN-NNN-NNNN``.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_OBFUSCATED = re.compile(r"\s*(\[at\]|\(at\)|\sat\s)\s*", re.IGNORECASE)


def normalize_phone(raw: str | None) -> str | None:
    """Return ``NNN-NNN-NNNN`` or None if not a valid US 10-digit number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01" or digits[3] in "01":  # invalid NANP area/exchange
        return None
    return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"


def normalize_email(raw: str | None) -> str | None:
    """Lowercase, strip, de-obfuscate, validate. Returns None if invalid."""
    if not raw:
        return None
    s = str(raw).strip()
    s = _OBFUSCATED.sub("@", s)
    s = s.replace(" ", "").replace("mailto:", "")
    s = s.lower()
    if not _EMAIL_RE.match(s):
        return None
    return s


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].lower()
