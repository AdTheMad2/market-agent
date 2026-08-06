"""Tests for engine.intraday, written before the implementation (TDD rule for `engine/`).

The rule this module exists to enforce is negative: **1-minute bars must never
reach `engine.triggers.evaluate`.** RSI(14) over fifteen one-minute closes
returns a plausible number that is pure noise, and that noise would consume a
slot of a ceiling of three (docs/IMPLEMENTATION_PLAN.md Task 4.1, constraint b).
So the intraday path gets its own comparison, and it compares prices to levels
and nothing else.

The substantive choice under test is that a touch is decided on each bar's
**high and low**, not its close. A level sitting at 350.00 that trades to 350.40
and closes back at 349.80 was touched; a close-to-close test would miss it, and
missing a touch is the one failure this system's user would never find out about.

See docs/SPEC.md §6.1.
"""

from __future__ import annotations

import pytest

from engine.intraday import Touch, touched_levels
from engine.triggers import Bar


def bar(t: str, low: float, high: float, close: float | None = None) -> Bar:
    close = high if close is None else close
    return Bar(t=t, o=low, h=high, l=low, c=close, v=10_000)


def window(*bars: Bar) -> list[Bar]:
    return list(bars)


# --------------------------------------------------------------------------
# The basic contract
# --------------------------------------------------------------------------


def test_no_bars_is_no_touch():
    assert touched_levels("GOOGL", [], [{"level": 350.0, "direction": "both"}]) == []


def test_no_armed_levels_is_no_touch():
    assert touched_levels("GOOGL", window(bar("t1", 349.0, 351.0)), []) == []


def test_a_level_inside_a_bar_range_is_touched():
    touches = touched_levels(
        "GOOGL", window(bar("t1", 349.5, 350.4, close=349.8)),
        [{"level": 350.0, "direction": "both"}],
    )
    assert len(touches) == 1
    assert touches[0].level == 350.0


def test_a_close_that_pulls_back_still_counts_as_a_touch():
    # The whole reason highs and lows are used instead of closes.
    touches = touched_levels(
        "GOOGL", window(bar("t1", 349.5, 350.4, close=349.8)),
        [{"level": 350.0, "direction": "both"}],
    )
    assert touches and touches[0].price == 349.8


def test_a_level_the_price_never_reached_is_not_touched():
    assert touched_levels(
        "GOOGL", window(bar("t1", 340.0, 342.0)), [{"level": 350.0, "direction": "both"}]
    ) == []


def test_the_touch_carries_the_timestamp_of_the_bar_that_touched_it():
    bars = window(
        bar("2026-08-06T14:31:00Z", 340.0, 341.0),
        bar("2026-08-06T14:32:00Z", 349.6, 350.2),
        bar("2026-08-06T14:33:00Z", 350.1, 351.0),
    )
    touches = touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "both"}])
    # The *first* bar that reached it, not the last bar of the window: the
    # message states when the level was touched, not when the poll ran.
    assert touches[0].bar_timestamp == "2026-08-06T14:32:00Z"


def test_the_touch_reports_the_latest_close_as_the_price():
    bars = window(
        bar("t1", 349.6, 350.2),
        bar("t2", 351.0, 352.0, close=351.5),
    )
    touches = touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "both"}])
    assert touches[0].price == 351.5


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_direction_above_fires_when_price_reaches_up_to_the_level():
    bars = window(bar("t1", 340.0, 350.5))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "above"}])


def test_direction_above_stays_silent_below_the_level():
    bars = window(bar("t1", 340.0, 345.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "above"}]) == []


def test_direction_below_fires_when_price_reaches_down_to_the_level():
    bars = window(bar("t1", 349.5, 355.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "below"}])


def test_direction_below_stays_silent_above_the_level():
    bars = window(bar("t1", 355.0, 360.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "below"}]) == []


def test_direction_above_still_fires_when_the_window_gapped_clear_of_the_level():
    # Price opened the window already through the level and never came back.
    # "Alert me if it goes past X" must not go silent because the move was fast.
    bars = window(bar("t1", 352.0, 355.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "above"}])


def test_both_requires_the_level_to_sit_inside_the_traded_range():
    bars = window(bar("t1", 352.0, 355.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "both"}]) == []


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_every_armed_level_is_reported_separately():
    bars = window(bar("t1", 340.0, 360.0))
    touches = touched_levels(
        "GOOGL",
        bars,
        [
            {"level": 345.0, "direction": "both"},
            {"level": 350.0, "direction": "both"},
            {"level": 355.0, "direction": "both"},
        ],
    )
    assert [t.level for t in touches] == [345.0, 350.0, 355.0]


def test_touches_are_returned_in_armed_order_not_price_order():
    bars = window(bar("t1", 340.0, 360.0))
    touches = touched_levels(
        "GOOGL", bars,
        [{"level": 355.0, "direction": "both"}, {"level": 345.0, "direction": "both"}],
    )
    assert [t.level for t in touches] == [355.0, 345.0]


def test_a_non_positive_level_is_skipped_rather_than_crashing_the_ticker():
    # `armed` is caller data out of SQLite; one bad row must not cost the other
    # levels, matching engine.triggers.evaluate's handling of the same input.
    bars = window(bar("t1", 340.0, 360.0))
    touches = touched_levels(
        "GOOGL", bars,
        [{"level": 0.0, "direction": "both"}, {"level": 350.0, "direction": "both"}],
    )
    assert [t.level for t in touches] == [350.0]


def test_an_unknown_direction_is_treated_as_both_rather_than_raising():
    bars = window(bar("t1", 349.0, 351.0))
    assert touched_levels("GOOGL", bars, [{"level": 350.0, "direction": "sideways"}])


def test_touch_is_frozen():
    touches = touched_levels(
        "GOOGL", window(bar("t1", 349.0, 351.0)), [{"level": 350.0, "direction": "both"}]
    )
    with pytest.raises(Exception):
        touches[0].level = 1.0  # type: ignore[misc]


def test_distance_pct_is_measured_from_the_level():
    touches = touched_levels(
        "GOOGL",
        window(bar("t1", 349.0, 351.0, close=353.5)),
        [{"level": 350.0, "direction": "both"}],
    )
    t: Touch = touches[0]
    assert t.distance_pct == pytest.approx(1.0)
