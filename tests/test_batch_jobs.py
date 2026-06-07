"""Offline tests for the pure batch job-planning helper."""

import pytest

from src.web.batch import build_jobs


def test_shared_prompt_expands_urls_into_jobs():
    jobs, combine = build_jobs(
        "bulk",
        urls=["https://a.com", " https://b.com ", "", "  "],
        prompt="agents: name, email",
        pairs=None,
    )
    assert combine == "stack"
    assert [j["url"] for j in jobs] == ["https://a.com", "https://b.com"]
    assert all(j["prompt"] == "agents: name, email" for j in jobs)


def test_queue_uses_per_url_prompts():
    jobs, combine = build_jobs(
        "queue",
        urls=None,
        prompt=None,
        pairs=[
            {"url": "https://a.com", "prompt": "agents: name"},
            {"url": " https://b.com ", "prompt": " listings: price "},
            {"url": "", "prompt": "ignored, no url"},
            {"url": "https://c.com", "prompt": ""},  # ignored, no prompt
        ],
    )
    assert combine == "sheets"
    assert jobs == [
        {"url": "https://a.com", "prompt": "agents: name"},
        {"url": "https://b.com", "prompt": "listings: price"},
    ]


def test_bulk_requires_urls_and_prompt():
    with pytest.raises(ValueError):
        build_jobs("bulk", urls=[], prompt="p", pairs=None)
    with pytest.raises(ValueError):
        build_jobs("bulk", urls=["https://a.com"], prompt="  ", pairs=None)


def test_queue_requires_at_least_one_valid_pair():
    with pytest.raises(ValueError):
        build_jobs("queue", urls=None, prompt=None, pairs=[{"url": "", "prompt": ""}])


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        build_jobs("nope", urls=["x"], prompt="p", pairs=None)
