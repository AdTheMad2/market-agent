"""FRED: the macro release calendar.

Used by the suppressors — a setup one day before CPI or an FOMC decision is
demoted, never promoted (docs/SPEC.md §5.3). Only the release *schedule* is
needed, not the series values, so this asks for release dates and names.

Which releases count is policy, not plumbing: the list lives in
`config/rules.yml` under `macro.watched_releases`, beside the
`suppressors.macro_event_within_days` window it pairs with.

Truncation is treated as an error rather than an empty calendar. The endpoint
returns dates for *every* release in the window and the watched-list filter runs
client-side, so hitting the server-side row cap silently drops the **latest**
dates under an ascending sort — and a suppressor that is absent is the worse of
the two failures (§5.3).

Never called from an intraday run: intraday fetches bars only (docs/SPEC.md §4.2).

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import requests  # noqa: F401 — imported so tests can patch the shared module attribute

from sources import MacroEvent, forbid_intraday, get_json, rules_config

URL = "https://api.stlouisfed.org/fred/releases/dates"
TIMEOUT = 20
PAGE_LIMIT = 1000


def watched_releases() -> tuple[str, ...]:
    """`macro.watched_releases` from config/rules.yml, lowercased for matching."""
    return tuple(str(name).lower() for name in rules_config()["macro"]["watched_releases"])


def _key() -> str:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY is not set; refusing to call the API")
    return key


def macro_calendar(days: int, watched: tuple[str, ...] | None = None) -> list[MacroEvent]:
    """Watched macro releases scheduled in the next `days` days, date-ascending."""
    forbid_intraday("the macro calendar")
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    terms = watched if watched is not None else watched_releases()
    today = datetime.now(UTC).date()
    events: list[MacroEvent] = []
    offset = 0

    while True:
        params = {
            "api_key": _key(),
            "file_type": "json",
            "realtime_start": today.isoformat(),
            "realtime_end": (today + timedelta(days=days)).isoformat(),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
        }
        # The API key is a query parameter — FRED offers no header form — so
        # `get_json` never surfaces the URL or the body in its error text.
        rows = get_json("FRED", URL, params=params, timeout=TIMEOUT).get("release_dates", [])
        events.extend(
            MacroEvent(
                date=row["date"],
                name=row.get("release_name", ""),
                release_id=str(row.get("release_id", "")),
            )
            for row in rows
            if _is_watched(row.get("release_name", ""), terms)
        )
        if len(rows) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

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
