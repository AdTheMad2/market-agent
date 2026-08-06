"""The calendar check. It exists so cron never has to know about holidays.

Crons run Monday to Friday; the NYSE does not. Without this check the system
sends a digest on Thanksgiving reporting Wednesday's bars as though they were
today's — the kind of wrongness that is only obvious to someone who already
knows the answer.

Dates below are real NYSE sessions and closures, chosen to cover a fixed
holiday, a moving one, and a same-week weekend boundary.

See docs/SPEC.md §3.1 and docs/IMPLEMENTATION_PLAN.md Task 3.3.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from jobs import market_calendar


def test_an_ordinary_weekday_is_a_trading_day():
    assert market_calendar.is_trading_day(date(2026, 8, 5)) is True


def test_a_weekend_is_not_a_trading_day():
    assert market_calendar.is_trading_day(date(2026, 8, 8)) is False
    assert market_calendar.is_trading_day(date(2026, 8, 9)) is False


def test_a_fixed_holiday_is_not_a_trading_day():
    # Independence Day 2025 fell on a Friday and the exchange closed.
    assert market_calendar.is_trading_day(date(2025, 7, 4)) is False


def test_a_moving_holiday_is_not_a_trading_day():
    # Thanksgiving 2025. A weekday, and cron would have fired.
    assert market_calendar.is_trading_day(date(2025, 11, 27)) is False


def test_previous_trading_day_skips_a_weekend():
    assert market_calendar.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)


def test_previous_trading_day_skips_a_holiday():
    assert market_calendar.previous_trading_day(date(2025, 11, 28)) == date(2025, 11, 26)


def test_is_trading_day_defaults_to_today_without_raising():
    assert market_calendar.is_trading_day() in (True, False)


# --------------------------------------------------------------------------
# is_market_open — only the intraday poll needs this one
# --------------------------------------------------------------------------


def test_market_is_open_mid_session():
    # 2026-08-05 is a Wednesday. 15:00 UTC is 11:00 ET, mid-session under EDT.
    assert market_calendar.is_market_open(datetime(2026, 8, 5, 15, 0, tzinfo=UTC)) is True


def test_market_is_shut_before_the_open():
    # 13:15 UTC is 09:15 ET — when the pre-market digest runs, by design.
    assert market_calendar.is_market_open(datetime(2026, 8, 5, 13, 15, tzinfo=UTC)) is False


def test_market_is_shut_after_the_close():
    # 20:15 UTC is 16:15 ET, fifteen minutes past the bell. The intraday cron
    # covers hour 20 to reach 16:00 ET under EST, so this poll must no-op.
    assert market_calendar.is_market_open(datetime(2026, 8, 5, 20, 15, tzinfo=UTC)) is False


def test_market_is_shut_on_a_holiday_during_session_hours():
    assert market_calendar.is_market_open(datetime(2025, 11, 27, 15, 0, tzinfo=UTC)) is False


def test_an_early_close_is_read_from_the_calendar_not_assumed():
    # The day after Thanksgiving 2025: the NYSE closed at 13:00 ET (18:00 UTC).
    assert market_calendar.is_market_open(datetime(2025, 11, 28, 17, 0, tzinfo=UTC)) is True
    assert market_calendar.is_market_open(datetime(2025, 11, 28, 19, 0, tzinfo=UTC)) is False


def test_a_naive_datetime_raises_rather_than_being_guessed():
    # Guessing the zone here would make the poll silently correct in one half of
    # the year and silently wrong in the other.
    with pytest.raises(ValueError):
        market_calendar.is_market_open(datetime(2026, 8, 5, 15, 0))


def test_session_bounds_is_none_when_there_is_no_session():
    assert market_calendar.session_bounds(date(2026, 8, 8)) is None
