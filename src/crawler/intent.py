"""Turn a natural-language prompt into a structured crawl intent.

This is the crawler's first LLM call (the first ``runCompletion`` in a FetchFox
trace): it reads the user's free-text request plus the seed URL and emits a
:class:`CrawlIntent` -- the target entity, the fields to collect, the target
states, and the URL/anchor keywords that steer the frontier toward roster pages
and away from noise. If the LLM is unavailable it falls back to a sensible
real-estate-agent default so the crawl still runs.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..models import EXPORT_HEADERS
from .llm import OllamaClient

log = logging.getLogger(__name__)

# Keywords that, by default, hint a link/page is (or is not) an agent roster.
_DEFAULT_INCLUDE = [
    "agent", "agents", "our-team", "our-agents", "find-an-agent", "roster",
    "directory", "realtor", "realtors", "staff", "team", "people", "associates",
    "sales-team", "meet", "professionals", "office", "page",
]
_DEFAULT_EXCLUDE = [
    "login", "signin", "sign-in", "register", "careers", "career", "blog",
    "news", "privacy", "terms", "cookie", "press", "investor", "listing",
    "property", "properties", "homes-for-sale", "rent", "mortgage", "cart",
    "checkout", "account", "advertise", "espanol", "javascript:", "mailto:",
    "tel:", ".pdf", ".jpg", ".png", ".css", ".js",
]


class CrawlIntent(BaseModel):
    """Structured description of what the crawl is looking for."""

    entity: str = "real estate agent"
    description: str = ""
    fields: list[str] = Field(default_factory=lambda: list(EXPORT_HEADERS))
    states: list[str] = Field(default_factory=list)
    # Generic "target segments": the distinct values the user wants results spread
    # ACROSS -- states, brands, categories, cities, anything the prompt enumerates.
    # This is the location-agnostic generalization of `states`; it drives result
    # filtering, balanced coverage, and URL proposal so the pipeline works for ANY
    # request, not just real-estate-by-state. Empty when the prompt enumerates no
    # targets. `segment_kind` is a short noun for what they are ("US state",
    # "brand", "category", ...).
    segments: list[str] = Field(default_factory=list)
    segment_kind: str = ""
    include_keywords: list[str] = Field(default_factory=lambda: list(_DEFAULT_INCLUDE))
    exclude_keywords: list[str] = Field(default_factory=lambda: list(_DEFAULT_EXCLUDE))
    seed_url: str = ""
    allowed_domains: list[str] = Field(default_factory=list)

    def merge_states(self, override: Optional[list[str]]) -> "CrawlIntent":
        if override:
            self.states = list(override)
            if not self.segments:  # explicit location override seeds the segments
                self.segments = list(override)
                self.segment_kind = self.segment_kind or "US state"
        return self


_SYSTEM = (
    "You are a web-crawl planner. Given a user's data-collection request and a "
    "seed URL, output ONLY a JSON object describing how to steer a crawler "
    "toward pages that list the target records. Keys:\n"
    '  "entity": short noun phrase for the record type (e.g. "real estate agent")\n'
    '  "fields": array of field names the user wants\n'
    '  "states": array of 2-letter US state codes mentioned, else []\n'
    '  "segments": array of the distinct target VALUES the user wants results '
    "spread across -- the states, brands, categories, cities, etc. they list "
    "(e.g. [\"NJ\",\"PA\",\"NY\"] or [\"Dell\",\"HP\"]); [] if the prompt names none\n"
    '  "segment_kind": a short noun for what those segments are (e.g. "US state", '
    '"brand", "city", "category"); "" if none\n'
    '  "include_keywords": lowercase substrings in URLs/links that likely lead '
    "to the target listing pages (e.g. agents, our-team, directory, roster)\n"
    '  "exclude_keywords": lowercase substrings to avoid (login, careers, blog, '
    "property listings, legal)\n"
    "Be concise; 6-15 keywords each. Output JSON only, no prose."
)


class Planner:
    def __init__(self, client: Optional[OllamaClient] = None):
        self.client = client

    def plan(self, prompt: str, seed_url: str,
             states: Optional[list[str]] = None) -> CrawlIntent:
        domain = urlparse(seed_url).netloc.lower()
        intent = CrawlIntent(seed_url=seed_url,
                             allowed_domains=[domain] if domain else [])
        if self.client is not None and self.client.available():
            data = self.client.chat_json(
                _SYSTEM,
                f"USER REQUEST:\n{prompt}\n\nSEED URL: {seed_url}",
                default=None,
            )
            if isinstance(data, dict):
                intent = self._merge_llm(intent, data)
            else:
                log.info("Planner: LLM gave no usable intent; using defaults.")
        else:
            log.info("Planner: no LLM; using default real-estate-agent intent.")
        intent.description = prompt.strip()
        return intent.merge_states(states)

    @staticmethod
    def _merge_llm(intent: CrawlIntent, data: dict) -> CrawlIntent:
        if data.get("entity"):
            intent.entity = str(data["entity"]).strip()
        if isinstance(data.get("fields"), list) and data["fields"]:
            intent.fields = [str(f).strip() for f in data["fields"] if str(f).strip()]
        if isinstance(data.get("states"), list) and data["states"]:
            intent.states = [str(s).strip().upper()[:2] for s in data["states"]]
        if isinstance(data.get("segments"), list) and data["segments"]:
            intent.segments = [str(s).strip() for s in data["segments"] if str(s).strip()]
        if data.get("segment_kind"):
            intent.segment_kind = str(data["segment_kind"]).strip()
        # Location prompts: if the model filled `states` but not `segments`, mirror
        # them so the generic machinery has something to work with.
        if not intent.segments and intent.states:
            intent.segments = list(intent.states)
            intent.segment_kind = intent.segment_kind or "US state"
        # Union LLM keywords with the robust defaults (defaults catch what the
        # model forgets; LLM adds site-specific hints).
        inc = {k.lower() for k in intent.include_keywords}
        for k in data.get("include_keywords", []) or []:
            inc.add(str(k).strip().lower())
        intent.include_keywords = sorted(k for k in inc if k)
        exc = {k.lower() for k in intent.exclude_keywords}
        for k in data.get("exclude_keywords", []) or []:
            exc.add(str(k).strip().lower())
        intent.exclude_keywords = sorted(k for k in exc if k)
        return intent
