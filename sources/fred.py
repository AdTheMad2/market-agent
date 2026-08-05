"""FRED: the macro release calendar.

Used by the suppressors — a setup one day before CPI or an FOMC decision is
demoted, never promoted (docs/SPEC.md §5.3). Only the release *schedule* is
needed, not the series values, so this asks for release dates and names.

The full FRED release list runs to hundreds of low-signal series. `WATCHED`
names the handful that actually move an equity book; everything else is
filtered out here rather than filling the digest.

Never called from an intraday run: intraday fetches bars only (docs/SPEC.md §4.2).

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import requests

from sources import MacroEvent, forbid_intraday

URL = "https://api.stlouisfed.org/fred/releases/dates"
TIMEOUT = 20

# Substring match, case-insensitive, against the release name. Deliberately
# short: a macro event only earns its place in a digest if it can move the
# whole tape.
WATCHED = (
    "consumer price index",
    "employment situation",
    "producer price index",
    "gross domestic product",
    "personal income and outlays",  # carries core PCE
    "fomc",
    "h.4.1",  # Fed balance sheet
    "retail sales",
)


def _key() -> str:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY is not set; refusing to call the API")
    return key


def macro_calendar(days: int, watched: tuple[str, ...] = WATCHED) -> list[MacroEvent]:
    """Watched macro releases scheduled in the next `days` days, date-ascending."""
    forbid_intraday("the macro calendar")
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    today = datetime.now(UTC).date()
    params = {
        "api_key": _key(),
        "file_type": "json",
        "realtime_start": today.isoformat(),
        "realtime_end": (today + timedelta(days=days)).isoformat(),
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": "1000",
    }
    try:
        response = requests.get(URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # The API key is a query parameter; the exception text would carry it.
        raise RuntimeError(f"FRED request failed: {type(exc).__name__}") from None
    if not response.ok:
        raise RuntimeError(f"FRED returned HTTP {response.status_code}")

    rows = response.json().get("release_dates", [])
    events = [
        MacroEvent(
            date=row["date"],
            name=row.get("release_name", ""),
            release_id=str(row.get("release_id", "")),
        )
        for row in rows
        if _is_watched(row.get("release_name", ""), watched)
    ]
    return sorted(events, key=lambda e: (e.date, e.name))


def _is_watched(name: str, watched: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(term in lowered for term in watched)


def dates_within(events: list[MacroEvent], days: int, today: date | None = None) -> list[MacroEvent]:
    """Events falling within `days` of `today` — the shape the suppressors want
    (`suppressors.macro_event_within_days`)."""
    start = today or datetime.now(UTC).date()
    end = start + timedelta(days=days)
    return [e for e in events if start.isoformat() <= e.date <= end.isoformat()]
