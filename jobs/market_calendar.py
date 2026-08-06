"""Is the NYSE open today? Asked in code, never encoded in a cron expression.

Crons here run Monday to Friday because cron cannot express "except holidays".
Without this check the post-close job would fire on Thanksgiving and deliver
Wednesday's bars as though they were today's — wrong in a way only a reader who
already knew the answer would catch (docs/SPEC.md §3.1).

`pandas_market_calendars` is imported lazily. It pulls pandas, which costs about
a second of import time, and `jobs/watchlist.py` and the renderer are imported by
tests that have no business paying for it.

Two questions live here, and they are not the same one. `is_trading_day` asks
whether a session exists — enough for the two digest runs, since the post-close
cron at 21:15 UTC is already past the 16:00 ET close on both EST and EDT and
early-close days end earlier still. `is_market_open` asks whether a session is
*in progress right now*, which only the intraday poll needs: its cron spans
14:00-20:59 UTC to cover both EST and EDT, so on any given day roughly an hour
of those polls falls outside the session and must do nothing.

`is_market_open` reads the calendar's own open and close times rather than
hardcoding 09:30-16:00, so the half-days around Thanksgiving and Christmas close
the poll at 13:00 ET without anyone maintaining a list of them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

EXCHANGE = "NYSE"

# How far back `previous_trading_day` will look before giving up. The longest
# closure in modern NYSE history was four days (Hurricane Sandy, 2012); a
# fortnight is generous and still bounded.
MAX_LOOKBACK_DAYS = 14


@lru_cache(maxsize=1)
def _calendar():
    import pandas_market_calendars as mcal

    return mcal.get_calendar(EXCHANGE)


@lru_cache(maxsize=32)
def _sessions(start: date, end: date) -> frozenset[date]:
    """Session dates in `[start, end]`. Cached: a job asks more than once."""
    days = _calendar().valid_days(start_date=start.isoformat(), end_date=end.isoformat())
    return frozenset(d.date() for d in days)


def is_trading_day(day: date | None = None) -> bool:
    """True when the NYSE holds a session on `day` (default: today, UTC).

    Today in UTC is the right default for this system's schedule: the pre-market
    (13:15 UTC) and post-close (21:15 UTC) runs both fall inside the same UTC
    calendar day as their ET session, under both EST and EDT.
    """
    target = day or datetime.now(UTC).date()
    return target in _sessions(target, target)


@lru_cache(maxsize=32)
def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """`(open, close)` in UTC for `day`, or `None` when there is no session.

    Read from the calendar rather than hardcoded, so an early close — the
    half-days around Thanksgiving and Christmas — ends the poll at 13:00 ET
    without anyone maintaining a list of them.
    """
    schedule = _calendar().schedule(start_date=day.isoformat(), end_date=day.isoformat())
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime()


def is_market_open(moment: datetime | None = None) -> bool:
    """True when `moment` (default: now, UTC) falls inside a live session.

    The intraday cron spans 14:00-20:59 UTC to cover the session under both EST
    and EDT, so about an hour of its polls land outside the session every day
    and have to be no-ops rather than fetches.
    """
    now = moment or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("moment must be timezone-aware; a naive datetime is unanswerable here")
    bounds = session_bounds(now.astimezone(UTC).date())
    if bounds is None:
        return False
    opened, closed = bounds
    return opened <= now < closed


def previous_trading_day(day: date | None = None) -> date:
    """The most recent session strictly before `day`.

    Raises rather than returning `None` when nothing is found inside
    `MAX_LOOKBACK_DAYS`: an empty calendar means the calendar data is broken,
    and a job that silently carried on would compare today's price against a
    level derived from nothing.
    """
    target = day or datetime.now(UTC).date()
    start = target - timedelta(days=MAX_LOOKBACK_DAYS)
    earlier = [d for d in _sessions(start, target - timedelta(days=1)) if d < target]
    if not earlier:
        raise RuntimeError(
            f"no {EXCHANGE} session in the {MAX_LOOKBACK_DAYS} days before {target.isoformat()}"
        )
    return max(earlier)
