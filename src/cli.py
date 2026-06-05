"""Command-line interface — LLM-guided discovery crawler.

    python -m src.main crawl --url https://example.com \\
        --prompt "agents: full name, email, phone, agency, city" --extract --export
"""

from __future__ import annotations

import argparse
import logging

from . import main as pipeline
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crawler")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser(
        "crawl",
        help="LLM-guided, free-form discovery from a seed URL + prompt",
    )
    c.add_argument("--url", required=True, help="seed URL to start crawling from")
    c.add_argument("--prompt", required=True, help="what to look for + fields (natural language)")
    c.add_argument("--states", nargs="+", default=None,
                   help="optional location hint (also just put it in the prompt)")
    c.add_argument("--max-pages", type=int, default=40, help="max pages to fetch (maxVisits)")
    c.add_argument("--max-depth", type=int, default=3, help="max link depth")
    c.add_argument("--max-extracts", type=int, default=10,
                   help="max discovered pages to extract from (maxExtracts)")
    c.add_argument("--cross-domain", action="store_true",
                   help="allow following links off the seed domain")
    c.add_argument("--extract", action="store_true",
                   help="run the LLM extractor on discovered pages")
    c.add_argument("--export", action="store_true",
                   help="export after extraction (implies --extract)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(level=logging.INFO)

    if args.command == "crawl":
        pipeline.crawl(
            url=args.url,
            prompt=args.prompt,
            states=args.states or [],
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            max_extracts=args.max_extracts,
            same_domain=not args.cross_domain,
            extract=args.extract or args.export,
            export_after=args.export,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
