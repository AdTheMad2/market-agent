"""Reading `config/watchlist_core.yml`. One implementation, used by every job.

The file is hand-edited, so every defect it can carry is a human one: a repeated
symbol, a lower-case ticker, a level written as a string, a commented-out block
left half-uncommented. Each is handled here rather than in the four callers.

Nothing personal may be read out of this file because nothing personal may be
written into it — the repository is public (docs/SPEC.md §9). Armed levels are
public market prices and carry no position information.

See docs/IMPLEMENTATION_PLAN.md Tasks 2.4 and 3.4.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist_core.yml"

VALID_DIRECTIONS = ("above", "below", "both")


def _load(path: Path | None = None) -> dict:
    return yaml.safe_load((path or WATCHLIST_PATH).read_text(encoding="utf-8")) or {}


def core_tickers(path: Path | None = None) -> list[str]:
    """The core watchlist, upper-cased and de-duplicated, in file order.

    De-duplication matters beyond tidiness: a repeated symbol would be fetched
    in two batches and its bars merged twice.
    """
    seen: set[str] = set()
    tickers: list[str] = []
    for raw in _load(path).get("tickers") or []:
        ticker = str(raw).upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def armed_levels(path: Path | None = None) -> list[dict]:
    """Hand-armed levels as `{ticker, level, direction}`, skipping malformed rows.

    A malformed entry is skipped rather than raising. This file is edited on a
    phone as often as not, and one bad row must not cost the whole digest — the
    other levels, and every rule-based trigger, are still worth delivering.
    """
    levels: list[dict] = []
    for raw in _load(path).get("armed_levels") or []:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker", "")).upper().strip()
        try:
            level = float(raw["level"])
        except (KeyError, TypeError, ValueError):
            continue
        if not ticker or level <= 0:
            continue
        direction = str(raw.get("direction", "both")).lower().strip()
        if direction not in VALID_DIRECTIONS:
            direction = "both"
        levels.append({"ticker": ticker, "level": level, "direction": direction})
    return levels
