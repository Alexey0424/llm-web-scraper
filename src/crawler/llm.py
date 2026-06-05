"""Thin client for a local Ollama LLM server.

Defaults to the ``gemma4:e4b`` edge model (effective 4B params, native
function-calling, 128K context) but any model is swappable via ``OLLAMA_MODEL``.
The server URL comes from ``OLLAMA_HOST`` (``http://ollama:11434`` inside the
compose network; ``http://localhost:11435`` for host-side use against the
project's isolated container).

The client is deliberately small: a JSON-mode chat call and a plain-text
generate call, with defensive JSON parsing. Every higher-level component
(planner, classifiers, extractor) degrades gracefully to heuristics when the
server is unreachable, so unit tests run with no LLM and no network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e4b"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_FIRST_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _strip_to_json(text: str) -> str:
    """Best-effort isolate a JSON object/array from a chatty model response."""
    t = text.strip()
    t = _FENCE_RE.sub("", t).strip()
    if t.startswith("{") or t.startswith("["):
        return t
    m = _FIRST_JSON_RE.search(t)
    return m.group(1) if m else t


class OllamaClient:
    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 180.0,
        num_ctx: int = 16384,
    ):
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.num_ctx = num_ctx
        self._client = httpx.Client(timeout=timeout)
        self.calls = 0  # observability: how many completions this client made
        # Optional progress hook: called after each completion with
        # {"kind": "runCompletion", "seconds": <float>}. Used to feed the UI's
        # FetchFox-style per-call timeline.
        self.on_call = None

    def _report(self, t0: float) -> None:
        if self.on_call:
            try:
                self.on_call({"kind": "runCompletion", "seconds": round(time.monotonic() - t0, 1)})
            except Exception:  # noqa: BLE001 - progress must never break a call
                pass

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- health --------------------------------------------------------------
    def available(self) -> bool:
        """True if the server answers and the configured model is present."""
        try:
            r = self._client.get(f"{self.host}/api/tags", timeout=5.0)
            r.raise_for_status()
            tags = {m.get("name", "") for m in r.json().get("models", [])}
        except Exception as exc:  # noqa: BLE001
            log.info("Ollama not available at %s: %s", self.host, exc)
            return False
        if self.model in tags:
            return True
        # Accept a bare family match (e.g. "gemma4:26b" vs "gemma4:26b-instruct").
        base = self.model.split(":")[0]
        return any(t.split(":")[0] == base for t in tags)

    # -- core calls ----------------------------------------------------------
    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        default: Any = None,
    ) -> Any:
        """Chat with ``format=json`` and return the parsed JSON (or ``default``)."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        t0 = time.monotonic()
        try:
            self.calls += 1
            r = self._client.post(f"{self.host}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Ollama chat failed: %s", exc)
            return default
        finally:
            self._report(t0)
        try:
            return json.loads(_strip_to_json(content))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("Ollama returned non-JSON (%s): %.200s", exc, content)
            return default

    def generate(self, prompt: str, *, system: Optional[str] = None,
                 temperature: float = 0.0) -> str:
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        if system:
            payload["system"] = system
        t0 = time.monotonic()
        try:
            self.calls += 1
            r = self._client.post(f"{self.host}/api/generate", json=payload)
            r.raise_for_status()
            return r.json().get("response", "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Ollama generate failed: %s", exc)
            return ""
        finally:
            self._report(t0)
