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

from datetime import date

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
