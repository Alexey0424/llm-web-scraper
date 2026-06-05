"""Phase-1 discovery crawler.

Given a seed URL and a natural-language prompt, a local LLM (Ollama / gemma4)
plans a crawl intent, then a polite same-domain BFS finds the pages that list the
target entity (real-estate agents). Those page URLs become a runtime
``source_cfg`` (``extractor: llm``) fed straight into the existing
:class:`src.main.Pipeline` -- so normalize / dedupe / score / 9-column Excel
export all work unchanged.

Compliance is inherited, not reinvented: every fetch goes through the same
``compliance.gate_url`` (robots.txt) + no-bypass policy as the rest of the
pipeline. Sites behind anti-bot / login simply fail that gate and are logged.
"""

from __future__ import annotations
