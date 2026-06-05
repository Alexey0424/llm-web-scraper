"""Provenance helpers: stamp records with source + timestamp."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import AgentRecord


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp(record: AgentRecord, *, source_name: str, source_url: str, snapshot_hash: str | None = None) -> AgentRecord:
    record.source_name = source_name
    record.source_url = source_url
    if source_url and source_url not in record.source_urls:
        record.source_urls.append(source_url)
    record.raw_snapshot_hash = snapshot_hash
    record.extracted_at = now_iso()
    return record
