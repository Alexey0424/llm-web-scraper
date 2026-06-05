"""Canonical data models for the scraper.

The in-memory record (:class:`AgentRecord`) is intentionally *richer* than the
Excel output contract: it carries provenance, confidence and audit fields so the
pipeline can dedupe, score and audit. The Excel export, however, writes only the
nine template columns defined in :data:`EXPORT_COLUMNS`, in that exact order.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# (model_field, template_header) in the EXACT order of the Excel template.
# Derived from `5 States (Real Estate Agents).xlsx`.
EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("full_name", "Agent Full Name"),
    ("first_name", "Agent First Name"),
    ("last_name", "Agent Last Name"),
    ("email", "Agent Email"),
    ("phone1", "Agent Phone 1"),
    ("phone2", "Agent Phone 2"),
    ("agency", "Agency Name"),
    ("city", "City"),
    ("state", "State"),
    ("source_url", "Source URL"),
]

EXPORT_FIELDS: list[str] = [f for f, _ in EXPORT_COLUMNS]
EXPORT_HEADERS: list[str] = [h for _, h in EXPORT_COLUMNS]


class AgentRecord(BaseModel):
    """A single real-estate agent contact record.

    The first nine fields map 1:1 to the client Excel template; ``source_url`` is
    appended as a tenth provenance column in the export (added for the discovery
    crawler, so each row records where it was found). The remaining fields below
    are internal quality/audit metadata and stay in the SQLite / JSONL outputs
    only, never in the Excel file.
    """

    # --- Template (exported) fields ---
    full_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone1: Optional[str] = None
    phone2: Optional[str] = None
    agency: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None

    # --- Internal provenance / quality (NOT exported to Excel) ---
    source_url: Optional[str] = None
    source_name: Optional[str] = None
    extracted_at: Optional[str] = None  # ISO8601 string
    confidence: float = 0.0
    confidence_bucket: Optional[str] = None  # high | medium | needs_review
    license_number: Optional[str] = None
    raw_snapshot_hash: Optional[str] = None
    # Provenance can accumulate across merged sources.
    source_urls: list[str] = Field(default_factory=list)
    notes: Optional[str] = None

    def export_row(self) -> list:
        """Return the nine template values in template order."""
        return [getattr(self, f) for f in EXPORT_FIELDS]

    def export_dict(self) -> dict:
        return {h: getattr(self, f) for f, h in EXPORT_COLUMNS}
