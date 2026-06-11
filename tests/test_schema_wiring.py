"""Every LLM call site passes a constraining JSON schema to chat_json.

Constrained decoding (Ollama ``format=<schema>``) makes wrong-keyed or
malformed replies impossible, so each call site must send the schema that
matches what it parses.
"""

from __future__ import annotations

from src.crawler.classifier import LinkClassifier, PageClassifier
from src.crawler.freeform import FreeFormExtractor
from src.crawler.frontier import Link
from src.crawler.intent import Planner


class FakeLLM:
    """Captures chat_json kwargs; replies from a canned queue (last reply sticks)."""

    model = "fake"
    calls = 0

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.captured: list[dict] = []

    def available(self) -> bool:
        return True

    def chat_json(self, system, user, **kw):
        self.captured.append({"system": system, "user": user, **kw})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _schema_of(fake: FakeLLM, i: int = 0) -> dict:
    assert fake.captured, "chat_json was never called"
    schema = fake.captured[i].get("schema")
    assert isinstance(schema, dict), "call site did not pass a schema"
    return schema


def test_freeform_extractor_sends_records_schema_from_columns():
    fake = FakeLLM([{"records": []}])
    cols = [("Full Name", "full_name"), ("Email", "email"), ("Source URL", "source_url")]
    FreeFormExtractor(cols, entity="agent", client=fake).extract(
        "<html><body>Maria</body></html>", base_url="http://x")
    schema = _schema_of(fake)
    item = schema["properties"]["records"]["items"]
    assert set(item["properties"]) == {"full_name", "email"}
    assert item["required"] == ["full_name", "email"]


def test_page_classifier_sends_verdict_schema():
    fake = FakeLLM([{"is_listing": True, "count": 2, "links_to_records": False,
                     "fields_present": [], "reason": "ok"}])
    intent = Planner(None).plan("agents", "http://x")
    PageClassifier(fake).classify("http://x/p", "<html><body>hi</body></html>", intent)
    schema = _schema_of(fake)
    assert {"is_listing", "count", "links_to_records",
            "fields_present", "reason"} <= set(schema["properties"])


def test_link_ranker_sends_scores_schema():
    fake = FakeLLM([{"scores": [{"i": 0, "p": 0.9}]}])
    intent = Planner(None).plan("agents", "http://x")
    LinkClassifier(fake).score_links(
        "http://x", [Link("http://x/agents", "Agents")], intent)
    schema = _schema_of(fake)
    assert "scores" in schema["properties"]


def test_propose_urls_sends_urls_schema():
    fake = FakeLLM([{"urls": ["http://x/a"]}])
    intent = Planner(None).plan("agents", "http://x")
    intent.segments = ["NJ"]
    LinkClassifier(fake).propose_urls("http://x", ["http://x/seen"], intent)
    schema = _schema_of(fake)
    assert schema["properties"]["urls"]["items"]["type"] == "string"


def test_detail_pattern_pick_sends_index_schema():
    fake = FakeLLM([{"index": 0}])
    intent = Planner(None).plan("agents", "http://x")
    links = [Link(f"http://x/profile/p{i}", "") for i in range(3)] + \
            [Link(f"http://x/city/c{i}", "") for i in range(3)]
    LinkClassifier(fake).detect_detail_pattern(links, intent)
    schema = _schema_of(fake)
    assert "index" in schema["properties"]


def test_planner_sends_plan_schema():
    fake = FakeLLM([{"entity": "agent", "fields": ["Name"], "states": [],
                     "segments": [], "segment_kind": "",
                     "include_keywords": [], "exclude_keywords": []}])
    Planner(fake).plan("collect agents", "http://x")
    schema = _schema_of(fake)
    assert {"entity", "fields", "segments"} <= set(schema["properties"])
