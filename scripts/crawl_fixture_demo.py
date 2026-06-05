"""End-to-end Phase-1 crawl against the local fixture brokerage site.

Serves tests/fixtures/crawl_site over real HTTP, then runs the full loop with a
real local LLM: plan intent -> discover roster URLs -> LLM-extract agents ->
dedupe/score -> export. Proves the FetchFox-style pipeline without depending on
any external website.

    docker compose run --rm --entrypoint python scraper scripts/crawl_fixture_demo.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, main as pipeline
from src.logging_setup import setup_logging

FIXTURES = config.PROJECT_ROOT / "tests" / "fixtures" / "crawl_site"
PORT = 8799


def serve() -> socketserver.TCPServer:
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(FIXTURES), **k
    )
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    setup_logging()
    config.ensure_dirs()
    httpd = serve()
    seed = f"http://127.0.0.1:{PORT}/index.html"
    # Free-form: the columns come from THIS sentence (note custom fields like
    # "brokerage" and "city" — whatever you ask for becomes the Excel columns).
    prompt = (
        "Find real estate agents in NJ. I need their full name, email address, "
        "phone number, brokerage name and city."
    )
    try:
        pipeline.crawl(
            url=seed, prompt=prompt, states=["NJ"],
            max_pages=15, max_depth=3,
            max_extracts=int(os.environ.get("MAX_EXTRACTS", "10")),
            extract=True, export_after=True,
        )
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
