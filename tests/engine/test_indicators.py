"""Tests for engine.indicators.

Every expected value here is hand-computed and the arithmetic is shown in a
comment. A test whose expectation was captured from the implementation asserts
only that the code still does what it did, which is precisely the bug class this
phase exists to catch: a wrong indicator produces a plausible alert that nobody
questions. See docs/IMPLEMENTATION_PLAN.md Task 1.1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.indicators import rsi, sma, volume_ratio

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "bars_goog.json"


@pytest.fixture(scope="module")
def goog_closes() -> list[float]:
    """Real Alpaca daily closes, oldest first. 250 sessions, committed."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [bar["c"] for bar in data["bars"]]


# --- sma ------------------------------------------------------------------


def test_sma_returns_none_when_insufficient_data():
    assert sma([1.0, 2.0], period=5) is None


def test_sma_returns_none_on_empty_input():
    assert sma([], period=5) is None


def test_sma_simple_case():
    # (1+2+3+4+5)/5 = 3.0
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0], period=5) == 3.0


def test_sma_uses_the_trailing_window_not_the_leading_one():
    # The last three of six are 4,5,6 -> (4+5+6)/3 = 5.0.
    # A leading-window bug would return (1+2+3)/3 = 2.0.
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], period=3) == 5.0


def test_sma_exact_length_is_sufficient():
    # period == len(closes) is enough; only fewer than period is None.
    assert sma([2.0, 4.0], period=2) == 3.0


def test_sma_on_real_bars_lies_within_the_window(goog_closes):
    # Not a hand-computed value: a mean must sit between the min and max of the
    # values it averages. Cheap guard against an off-by-one window on real data.
    window = goog_closes[-150:]
    value = sma(goog_closes, period=150)
    assert value is not None
    assert min(window) <= value <= max(window)


# --- rsi ------------------------------------------------------------------


def test_rsi_returns_none_when_insufficient_data():
    # 14 closes gives 13 changes; Wilder's seed needs `period` changes, so 15
    # closes is the minimum for period=14.
    assert rsi([float(i) for i in range(1, 15)], period=14) is None


def test_rsi_all_gains_is_100():
    # No losses at all -> average loss 0 -> RSI is 100 by definition, and the
    # implementation must not divide by zero to get there.
    closes = [float(i) for i in range(1, 20)]
    assert rsi(closes, period=14) == 100.0


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(20, 1, -1)]
    assert rsi(closes, period=14) == 0.0


def test_rsi_wilder_smoothing_hand_computed():
    # closes:  10 -> 11 -> 10.5 -> 11.5
    # changes: +1.0, -0.5, +1.0
    #
    # Seed over the first period=2 changes:
    #   avg_gain = (1.0 + 0.0) / 2 = 0.5
    #   avg_loss = (0.0 + 0.5) / 2 = 0.25
    #
    # Wilder smoothing of the third change (+1.0, so gain=1.0, loss=0.0):
    #   avg_gain = (0.5  * (2-1) + 1.0) / 2 = 0.75
    #   avg_loss = (0.25 * (2-1) + 0.0) / 2 = 0.125
    #
    # RS  = 0.75 / 0.125 = 6.0
    # RSI = 100 - 100/(1+6) = 100 - 14.285714... = 85.714285...
    #
    # A simple-average implementation would give a different answer here, which
    # is the point of this case.
    result = rsi([10.0, 11.0, 10.5, 11.5], period=2)
    assert result == pytest.approx(85.714285, abs=1e-5)


def test_rsi_flat_prices_is_50():
    # No gains and no losses. Both averages are zero, which is neither the
    # all-gain nor the all-loss case; 50 is the only defensible answer and the
    # code must not raise.
    assert rsi([100.0] * 20, period=14) == 50.0


def test_rsi_on_real_bars_is_within_bounds(goog_closes):
    value = rsi(goog_closes, period=14)
    assert value is not None
    assert 0.0 <= value <= 100.0


# --- volume_ratio ---------------------------------------------------------


def test_volume_ratio_excludes_latest_from_baseline():
    # baseline is the twenty 100s; latest is 250 -> 250/100 = 2.5.
    # Including the latest in the baseline would give 250/107.14 = 2.33.
    assert volume_ratio([100] * 20 + [250], period=20) == 2.5


def test_volume_ratio_returns_none_when_insufficient_data():
    # period=20 needs 20 baseline values plus the latest = 21.
    assert volume_ratio([100] * 20, period=20) is None


def test_volume_ratio_returns_none_on_zero_baseline():
    # A halted or newly listed symbol can have zero volume across the window.
    # None is correct; a ZeroDivisionError in the middle of a scheduled run is not.
    assert volume_ratio([0] * 20 + [250], period=20) is None


def test_volume_ratio_of_average_volume_is_one():
    assert volume_ratio([100] * 20 + [100], period=20) == 1.0


def test_volume_ratio_on_real_bars_is_positive(goog_closes):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    volumes = [bar["v"] for bar in data["bars"]]
    value = volume_ratio(volumes, period=20)
    assert value is not None
    assert value > 0
