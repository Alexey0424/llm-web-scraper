"""Logging configuration: console + per-run file."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import RUNS_DIR


def setup_logging(level: int = logging.INFO, run_id: str | None = None) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = RUNS_DIR / f"{run_id}.log"

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on repeated setup.
    for h in list(root.handlers):
        root.removeHandler(h)

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    return log_path
