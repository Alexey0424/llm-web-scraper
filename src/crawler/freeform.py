"""Free-form LLM extraction: pull whatever fields the prompt asked for.

Returns plain dicts keyed by prompt-derived column keys. The columns are
decided per crawl by :mod:`src.crawler.fields`, so the same engine works for
any record type the user describes — there is no fixed schema.
"""

from __future__ import annotations

import logging
import re

from ..extractors.llm_extractor import _chunks, html_to_signal_text
from ..normalizers.phone_email_normalizer import normalize_email, normalize_phone
from .jsonld import extract_contact_fields
from .llm import OllamaClient

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _records_schema(keys: list[str]) -> dict:
    """JSON schema for the extraction reply: constrained decoding makes wrong
    keys / malformed JSON impossible (Ollama ``format=<schema>``)."""
    return {
        "type": "object",
        "properties": {"records": {"type": "array", "items": {
            "type": "object",
            "properties": {k: {"type": ["string", "null"]} for k in keys},
            "required": list(keys),
        }}},
        "required": ["records"],
    }


def _tokens(s) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))


def _name_match(ld_name: str, row: dict, name_keys: list[str]) -> bool:
    """Loose token match between a JSON-LD name and a record's name field(s).

    Catches LLM variants of the same entity ("Maria J DePasquale" vs "Maria
    DePasquale") while rejecting unrelated names (a footer org vs an agent).
    """
    t = _tokens(ld_name)
    if not t:
        return False
    for k in name_keys:
        v = row.get(k)
        if not v:
            continue
        rt = _tokens(v)
        if rt and (t <= rt or rt <= t or len(t & rt) >= 2):
            return True
    return False


_PHONE_KEY_TOKENS = ("phone", "tel", "cell", "mobile")
# Column-key tokens that mark a *organization* name column ("agency_name"),
# which must never be treated as the record's person-name field.
_ORG_NAME_TOKENS = ("agency", "company", "brokerage", "organization", "office",
                    "firm", "business", "brand", "team")
_INITIAL_RE = re.compile(r"[A-Za-z]\.?")
_OBFUSCATED_AT_RE = re.compile(r"\s*(\[at\]|\(at\)|\sat\s)\s*", re.IGNORECASE)


def _person_name_keys(keys: list[str]) -> list[str]:
    return [k for k in keys if "name" in k and "first" not in k and "last" not in k
            and not any(t in k for t in _ORG_NAME_TOKENS)]


def _derive_name_fields(row: dict, keys: list[str]) -> dict:
    """Fill first/last from a full name (and full from first+last).

    Sources frequently publish only one of the two forms; when the prompt asks
    for both, the missing one is derived deterministically (a single middle
    initial is skipped, multi-word last names are kept whole).
    """
    fulls = _person_name_keys(keys)
    firsts = [k for k in keys if "name" in k and "first" in k]
    lasts = [k for k in keys if "name" in k and "last" in k]
    full_val = next((row[k] for k in fulls if row.get(k)), None)
    first_val = next((row[k] for k in firsts if row.get(k)), None)
    last_val = next((row[k] for k in lasts if row.get(k)), None)
    if full_val:
        toks = full_val.split()
        if len(toks) >= 2:
            first, rest = toks[0], toks[1:]
            if len(rest) >= 2 and _INITIAL_RE.fullmatch(rest[0]):
                rest = rest[1:]
            for k in firsts:
                if not row.get(k):
                    row[k] = first
            for k in lasts:
                if not row.get(k):
                    row[k] = " ".join(rest)
    elif first_val or last_val:
        joined = " ".join(x for x in (first_val, last_val) if x)
        for k in fulls:
            if not row.get(k):
                row[k] = joined
    return row


def _email_grounded(v: str, src: str) -> bool:
    return v.lower() in src


def _phone_grounded(v: str, src: str) -> bool:
    digits = re.sub(r"\D", "", v)
    if len(digits) < 7:
        return True  # too short to judge reliably
    pat = r"\D{0,3}".join(re.escape(d) for d in digits)
    return re.search(pat, src) is not None


def _name_grounded(v: str, src: str) -> bool:
    toks = [t for t in re.findall(r"[a-z0-9]+", v.lower()) if len(t) > 1]
    if not toks:
        return True
    hits = sum(1 for t in toks if t in src)
    return hits >= 1 and hits * 2 >= len(toks)


def _ground_row(row: dict, keys: list[str], src: str) -> dict:
    """Blank email/phone/name values that cannot be located in the page.

    A small model occasionally invents plausible contact data; an extraction
    that can't be found in the source is treated as hallucinated (the
    LangExtract pattern). Format differences are tolerated: phones match on
    digits with separators allowed, names on token majority.
    """
    for k in keys:
        v = row.get(k)
        if not v:
            continue
        if "email" in k and not _email_grounded(v, src):
            row[k] = None
        elif any(t in k for t in _PHONE_KEY_TOKENS) and not _phone_grounded(v, src):
            row[k] = None
        elif ("name" in k and "first" not in k and "last" not in k
              and not _name_grounded(v, src)):
            row[k] = None
    return row


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
        keys = [k for _, k in self.columns]
        # Structured fields from schema.org JSON-LD (name/telephone/email/address).
        # Reliable on detail pages and present even when the visible page hides
        # contact info -- this is how email/phone get filled.
        ld = extract_contact_fields(content)
        # Page furniture (breadcrumbs, WebSite) must never name a record —
        # a breadcrumb's "New Jersey" is not an entity.
        ld_cat = ld.get("_name_type", "other")
        if not self.client.available():
            log.warning("FreeFormExtractor: Ollama unavailable; JSON-LD only.")
            fill = dict(ld)
            if ld_cat == "site":
                fill.pop("name", None)
            row = self._fill_from_jsonld({k: None for k in keys}, fill)
            row = _derive_name_fields(row, keys)
            return [row] if any(row.values()) else []
        text = html_to_signal_text(content)
        # Grounding source: raw HTML + the marker-inlined text (markers carry
        # decoded mailto emails that may be URL-encoded in the raw HTML) + a
        # de-obfuscated variant so a normalized "x@y.com" still matches a page
        # that printed "x [at] y.com".
        ground_src = ((content or "").lower() + "\n" + text.lower() + "\n"
                      + _OBFUSCATED_AT_RE.sub("@", text.lower()))
        out: list[dict] = []
        seen: set[str] = set()
        system = self._system()
        schema = _records_schema(keys)
        chunks = _chunks(text)
        if len(chunks) > self.max_chunks:
            log.info("FreeFormExtractor: page text %d chars -> %d chunks; "
                     "extracting only the first %d (%s)", len(text), len(chunks),
                     self.max_chunks, base_url)
        for chunk in chunks[: self.max_chunks]:
            data = self.client.chat_json(
                system, f"PAGE URL: {base_url}\n\nPAGE TEXT:\n{chunk}",
                default=None, schema=schema,
            )
            records = data.get("records") if isinstance(data, dict) else None
            for raw in records or []:
                if not isinstance(raw, dict):
                    continue
                row = {k: _clean(raw.get(k)) for k in keys}
                for k in keys:
                    if not row.get(k):
                        continue
                    # Salvage a clean email out of a noisy value, then
                    # normalize (de-obfuscate, lowercase, validate).
                    if "email" in k:
                        m = _EMAIL_RE.search(row[k])
                        cand = m.group(0) if m else row[k]
                        row[k] = normalize_email(cand) or cand
                    # Normalize phones to NNN-NNN-NNNN; keep the raw value if it
                    # isn't a parseable US number (extension, international, ...).
                    elif "phone" in k or "tel" in k or "cell" in k or "mobile" in k:
                        row[k] = normalize_phone(row[k]) or row[k]
                row = _ground_row(row, keys, ground_src)
                if not any(row.values()):
                    continue
                sig = "|".join((row.get(k) or "").lower() for k in keys)
                if sig in seen:
                    continue
                seen.add(sig)
                out.append(row)
        # A page carrying a single schema.org entity (name) is one record's detail
        # page: merge the LLM's possibly-duplicated/partial rows + structured
        # contact into ONE record, so we don't emit "Maria J DePasquale" AND
        # "Maria DePasquale" and so email/phone are filled from JSON-LD/mailto.
        # Guard rails: never collapse on the strength of page furniture (site),
        # and only collapse when the JSON-LD name actually denotes one of the
        # extracted records (or the page yielded none) — otherwise a footer org
        # or breadcrumb would silently flatten a whole roster into one row.
        name_keys = _person_name_keys(keys) or keys
        ld_name = ld.get("name")
        if ld_name and ld_cat != "site" and (
                ld_cat == "person" or not out
                or any(_name_match(ld_name, r, name_keys) for r in out)):
            primary = [r for r in out if _name_match(ld_name, r, name_keys)] or out
            merged = {k: None for k in keys}
            for r in primary:
                for k in keys:
                    if not merged.get(k) and r.get(k):
                        merged[k] = r[k]
            merged = self._fill_from_jsonld(merged, ld)
            out = [merged] if any(merged.values()) else []
        elif ld and len(out) == 1:
            # Page-level contact (org JSON-LD, mailto/tel links) is only safe to
            # fill into a SINGLE record; spraying it across a roster invents
            # data and later collapses distinct people during phone/email dedup.
            fill = dict(ld)
            if ld_cat in ("org", "site"):
                fill.pop("name", None)
            out = [self._fill_from_jsonld(out[0], fill)]
        out = [_derive_name_fields(r, keys) for r in out]
        log.info("FreeFormExtractor: %d records from %s", len(out), base_url)
        return out

    def _fill_from_jsonld(self, row: dict, ld: dict) -> dict:
        """Fill empty record fields from JSON-LD values, by column-key semantics.

        JSON-LD carries a single telephone, so it fills only the FIRST empty
        phone column (e.g. Phone 1), never duplicating into Phone 2.
        """
        phone_used = False
        for _, k in self.columns:
            if row.get(k):
                if any(t in k for t in ("phone", "tel", "cell", "mobile")):
                    phone_used = True
                continue
            if "email" in k and ld.get("email"):
                row[k] = ld["email"]
            elif (not phone_used
                  and any(t in k for t in ("phone", "tel", "cell", "mobile"))
                  and ld.get("telephone")):
                row[k] = normalize_phone(ld["telephone"]) or ld["telephone"]
                phone_used = True
            elif any(t in k for t in _ORG_NAME_TOKENS) and ld.get("org"):
                if "name" in k or k in ("agency", "company", "brokerage",
                                        "organization", "firm", "office"):
                    row[k] = ld["org"]
            elif ("name" in k and "first" not in k and "last" not in k
                  and not any(t in k for t in _ORG_NAME_TOKENS) and ld.get("name")):
                row[k] = ld["name"]
            elif k == "city" and ld.get("city"):
                row[k] = ld["city"]
            elif k == "state" and ld.get("state"):
                row[k] = ld["state"]
        return row
