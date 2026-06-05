"""Free-form LLM extraction: pull whatever fields the prompt asked for.

Unlike the fixed-schema ``LlmExtractor`` (which mapped onto the 9-column
AgentRecord), this returns plain dicts keyed by the prompt-derived column keys.
The columns are decided per crawl by :mod:`src.crawler.fields`, so the same
engine works for real-estate agents or anything else the user describes.
"""

from __future__ import annotations

import logging
import re

from ..extractors.llm_extractor import _chunks, html_to_signal_text
from ..normalizers.phone_email_normalizer import normalize_phone
from .llm import OllamaClient

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _clean(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        v = " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown", "-", ""}:
        return None
    return s


class FreeFormExtractor:
    """Extract records as dicts keyed by ``columns`` (excluding source_url)."""

    def __init__(self, columns: list[tuple[str, str]], entity: str = "record",
                 client: OllamaClient | None = None, max_chunks: int = 4):
        # columns includes the trailing source_url; we extract everything but it.
        self.columns = [(h, k) for h, k in columns if k != "source_url"]
        self.entity = entity or "record"
        self.client = client or OllamaClient()
        self.max_chunks = max_chunks

    def _system(self) -> str:
        spec = ", ".join(f'"{k}" ({h})' for h, k in self.columns)
        keys = ", ".join(f'"{k}"' for _, k in self.columns)
        return (
            f"You extract structured records about: {self.entity}. From the page "
            f"text, return ONLY JSON of the form {{\"records\": [{{{keys}}}, ...]}}. "
            f"Each record's keys MUST be exactly: {spec}. Rules: include an item "
            f"only if it is a real {self.entity} with at least one non-empty field; "
            "use null for unknown fields; copy emails/phones EXACTLY as written "
            "(look for [email: ...] and [phone: ...] markers); never invent data. "
            "If the page has none, return {\"records\": []}."
        )

    def extract(self, content: str, *, base_url: str = "") -> list[dict]:
        if not self.client.available():
            log.warning("FreeFormExtractor: Ollama unavailable; no records.")
            return []
        text = html_to_signal_text(content)
        keys = [k for _, k in self.columns]
        out: list[dict] = []
        seen: set[str] = set()
        system = self._system()
        for chunk in _chunks(text)[: self.max_chunks]:
            data = self.client.chat_json(
                system, f"PAGE URL: {base_url}\n\nPAGE TEXT:\n{chunk}", default=None
            )
            records = data.get("records") if isinstance(data, dict) else None
            for raw in records or []:
                if not isinstance(raw, dict):
                    continue
                row = {k: _clean(raw.get(k)) for k in keys}
                for k in keys:
                    if not row.get(k):
                        continue
                    # Salvage a clean email out of a noisy value.
                    if "email" in k and not _EMAIL_RE.fullmatch(row[k]):
                        m = _EMAIL_RE.search(row[k])
                        row[k] = m.group(0) if m else row[k]
                    # Normalize phones to NNN-NNN-NNNN; keep the raw value if it
                    # isn't a parseable US number (extension, international, ...).
                    elif "phone" in k or "tel" in k or "cell" in k or "mobile" in k:
                        row[k] = normalize_phone(row[k]) or row[k]
                if not any(row.values()):
                    continue
                sig = "|".join((row.get(k) or "").lower() for k in keys)
                if sig in seen:
                    continue
                seen.add(sig)
                out.append(row)
        log.info("FreeFormExtractor: %d records from %s", len(out), base_url)
        return out
