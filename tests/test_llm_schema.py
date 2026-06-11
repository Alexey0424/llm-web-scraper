"""OllamaClient: JSON-schema-constrained output + bounded retry on bad JSON."""

from __future__ import annotations

import json

from src.crawler.llm import OllamaClient


class _Resp:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": self._content}}


class _Http:
    """Stub httpx client: returns queued response bodies, records payloads."""

    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.payloads: list[dict] = []

    def post(self, url, json=None):  # noqa: A002 - mirror httpx signature
        self.payloads.append(json)
        return _Resp(self.contents.pop(0))


SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {"type": "object"}}},
    "required": ["records"],
}


def _client(contents: list[str]) -> tuple[OllamaClient, _Http]:
    c = OllamaClient(host="http://stub:1", model="stub")
    http = _Http(contents)
    c._client = http
    return c, http


def test_schema_is_sent_as_format():
    c, http = _client(['{"records": []}'])
    out = c.chat_json("sys", "user", schema=SCHEMA)
    assert out == {"records": []}
    assert http.payloads[0]["format"] == SCHEMA


def test_without_schema_format_stays_json():
    c, http = _client(['{"a": 1}'])
    c.chat_json("sys", "user")
    assert http.payloads[0]["format"] == "json"


def test_retries_once_on_invalid_json_with_corrective_message():
    c, http = _client(["not json at all", '{"records": [{"x": 1}]}'])
    out = c.chat_json("sys", "user", schema=SCHEMA)
    assert out == {"records": [{"x": 1}]}
    assert len(http.payloads) == 2
    # The retry must tell the model what went wrong (carries the prior reply).
    msgs = http.payloads[1]["messages"]
    assert any(m["role"] == "assistant" for m in msgs)
    assert "json" in msgs[-1]["content"].lower()
    assert c.calls == 2  # both completions counted for observability


def test_retry_exhausted_returns_default():
    c, http = _client(["garbage", "more garbage"])
    out = c.chat_json("sys", "user", default={"records": []})
    assert out == {"records": []}
    assert len(http.payloads) == 2  # bounded: exactly one retry


def test_valid_first_answer_does_not_retry():
    c, http = _client([json.dumps({"ok": True})])
    assert c.chat_json("sys", "user") == {"ok": True}
    assert len(http.payloads) == 1
