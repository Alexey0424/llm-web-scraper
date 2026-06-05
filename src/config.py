"""Configuration loading: YAML config files + environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Project root = parent of the `src` package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = PROJECT_ROOT / "runs"
DOCS_DIR = PROJECT_ROOT / "docs"

TEMPLATE_FILE = PROJECT_ROOT / "5 States (Real Estate Agents).xlsx"
SQLITE_PATH = DATA_DIR / "processed" / "agents.sqlite"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def load_states() -> dict[str, Any]:
    return _load_yaml("states.yaml")


@lru_cache(maxsize=None)
def load_rate_limits() -> dict[str, Any]:
    return _load_yaml("rate_limits.yaml")


@lru_cache(maxsize=None)
def load_sources() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


@lru_cache(maxsize=None)
def load_field_mapping() -> dict[str, Any]:
    return _load_yaml("field_mapping.yaml")


def state_name_to_code() -> dict[str, str]:
    return {k.lower(): v for k, v in load_states().get("state_name_to_code", {}).items()}


def target_state_codes() -> list[str]:
    return [s["code"] for s in load_states().get("states", [])]


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def ensure_dirs() -> None:
    """Create the data/runs directory tree if missing."""
    for sub in ["raw", "processed", "exports", "cache"]:
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
