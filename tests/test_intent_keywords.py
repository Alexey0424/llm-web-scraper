"""Keyword steering must follow the prompt, not the legacy real-estate defaults.

With no LLM the real-estate defaults remain a sensible fallback; when the LLM
plans the crawl, its keywords drive steering (plus a small entity-neutral
exclude list), and any exclude that contradicts the user's own prompt is
dropped — asking for "property listings" must not auto-exclude "listing".
"""

from __future__ import annotations

from src.crawler.intent import Planner


class FakeLLM:
    model = "fake"
    calls = 0

    def __init__(self, reply):
        self.reply = reply

    def available(self):
        return True

    def chat_json(self, system, user, **kw):
        return self.reply


def test_no_llm_keeps_default_keywords():
    intent = Planner(None).plan("agents", "http://x")
    assert "agents" in intent.include_keywords
    assert "login" in intent.exclude_keywords


def test_llm_plan_does_not_union_real_estate_includes():
    fake = FakeLLM({"entity": "laptop", "fields": ["Model", "Price"],
                    "states": [], "segments": [], "segment_kind": "",
                    "include_keywords": ["laptop", "laptops", "product"],
                    "exclude_keywords": ["blog"]})
    intent = Planner(fake).plan("collect laptops with prices", "http://shop.example")
    assert "laptop" in intent.include_keywords
    assert "realtor" not in intent.include_keywords
    assert "our-team" not in intent.include_keywords


def test_llm_plan_gets_neutral_excludes_plus_its_own():
    fake = FakeLLM({"entity": "laptop", "fields": [], "states": [],
                    "segments": [], "segment_kind": "",
                    "include_keywords": ["laptop"], "exclude_keywords": ["blog"]})
    intent = Planner(fake).plan("collect laptops", "http://shop.example")
    assert "blog" in intent.exclude_keywords
    assert "login" in intent.exclude_keywords  # neutral safety net
    assert ".pdf" in intent.exclude_keywords


def test_excludes_conflicting_with_prompt_are_dropped():
    fake = FakeLLM({"entity": "property listing", "fields": ["Address", "Price"],
                    "states": [], "segments": [], "segment_kind": "",
                    "include_keywords": ["listings", "for-sale"],
                    "exclude_keywords": ["listing", "property", "blog"]})
    intent = Planner(fake).plan(
        "collect property listings for sale with address and price",
        "http://realty.example")
    assert "listing" not in intent.exclude_keywords
    assert "property" not in intent.exclude_keywords
    assert "blog" in intent.exclude_keywords
    assert "login" in intent.exclude_keywords


def test_real_estate_excludes_do_not_leak_into_llm_plans():
    fake = FakeLLM({"entity": "rental listing", "fields": [], "states": [],
                    "segments": [], "segment_kind": "",
                    "include_keywords": ["rentals"], "exclude_keywords": []})
    intent = Planner(fake).plan("find apartments for rent", "http://x")
    # The legacy defaults exclude "rent" / "property" — fatal for this prompt.
    assert "rent" not in intent.exclude_keywords
    assert "homes-for-sale" not in intent.exclude_keywords
