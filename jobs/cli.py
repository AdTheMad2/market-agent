"""Argument parsing and process setup shared by every scheduled job.

Small enough to inline into each job and duplicated often enough that inlining
it would guarantee the day one job gains `--dry-run` and the other does not.

`--day` exists so a missed run can be re-run for the day it missed, and so the
jobs are testable without freezing the clock.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from scripts._dotenv import load_env
from sources import store

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(doc: str | None, argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument("--db", default=str(store.DEFAULT_DB_PATH), help="SQLite state file")
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        default=None,
        help="trading day to run for, YYYY-MM-DD (default: today, UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message instead of sending it, and write no sent record",
    )
    parser.add_argument(
        "--test-chat",
        action="store_true",
        help="send to TELEGRAM_TEST_CHAT_ID rather than the live chat",
    )
    return parser.parse_args(argv)


def prepare() -> None:
    """Console encoding and local credentials.

    The Windows console defaults to cp1252 and raises on the em dash the
    renderer uses. In Actions the environment already carries the secrets and
    `.env` does not exist, so `load_env` is a no-op there by design.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env(REPO_ROOT / ".env")
