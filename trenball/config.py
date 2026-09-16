"""Paths and default settings for trenball auto-track."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
CONFIG_PATH = ROOT / "trenball_config.json"
CAPTURES_DIR = ROOT / "captures"
HISTORY_PATH = ROOT / "trenball_history.json"

DEFAULT_POLL_SECONDS = 20
GRID_ROWS = 5


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {
            "region": None,
            "poll_seconds": DEFAULT_POLL_SECONDS,
            "grid_rows": GRID_ROWS,
        }
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("poll_seconds", DEFAULT_POLL_SECONDS)
    data.setdefault("grid_rows", GRID_ROWS)
    return data


def save_config(data: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
