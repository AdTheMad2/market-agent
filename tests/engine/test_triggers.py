"""Tests for engine.triggers.

Written before the implementation (workspace TDD rule for `engine/`). Bar
fixtures below are synthetic and built by hand so each test isolates exactly
one rule; `rules` loads the real config/rules.yml so these tests break the
moment a threshold key is renamed or removed from that file.

Where a fixture's SMA/RSI value is asserted, the number was computed by
calling the already-tested, already-committed `engine.indicators` functions
directly against the candidate closing prices while designing the fixture
(never captured from `engine.triggers.evaluate`, the code under test) and is
reproduced verbatim in the comment beside the assertion.

See docs/SPEC.md Section 5.2 and docs/IMPLEMENTATION_PLAN.md Task 1.2.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.triggers import Bar, Rules, evaluate

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yml"


@pytest.fixture(scope="module")
def rules() -> Rules:
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return Rules.from_mapping(data)


def bars_from_closes(closes: list[float], volume: list[int] | int = 1_000_000) -> list[Bar]:
    """Each close becomes a flat bar (o=h=l=c) on its own day. Good enough for
    every rule here except range_break, which cares about high/low: those
    tests build bars directly instead.
    """
    volumes = volume if isinstance(volume, list) else [volume] * len(closes)
    return [
        Bar(t=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", o=c, h=c, l=c, c=c, v=v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def oscillating(center: float, count: int) -> list[float]:
    """`count` closes alternating center +/- 0.05, so a trailing window of
    even length averages to exactly `center` and RSI stays near 50 -- a
    baseline that does not itself trip any rule, used to isolate the rule
    under test.
    """
    return [center + (0.05 if i % 2 == 0 else -0.05) for i in range(count)]


def flat_bars(price: float, count: int, volume: int = 1_000_000) -> list[Bar]:
    return bars_from_closes([price] * count, volume=volume)


# --- ma_proximity -------------------------------------------------------------


def test_ma_proximity_fires_within_threshold(rules):
    # 148 oscillating closes around 100.0 (cancels to sma == 100.0 over any
    # even trailing window) followed by two closes at 100.4. sma_50 == 100.016,
    # sma_150 == 100.005333 (150 bars total; sma_200 is None, insufficient
    # data). 100.4 is ~0.38-0.39% from each -- inside the 1.0% band. Previous
    # and current close are both 100.4 and both already above every MA, so no
    # ma_cross fires alongside it.
    closes = oscillating(100.0, 148) + [100.4, 100.4]
    bars = bars_from_closes(closes)
    triggers = evaluate("GOOG", bars, rules, armed=[])
    assert [t.rule for t in triggers] == ["ma_proximity", "ma_proximity"]
    assert triggers[0].level == pytest.approx(100.016, abs=1e-6)
    assert triggers[1].level == pytest.approx(100.005333, abs=1e-5)
    assert triggers[0].price == 100.4
    assert triggers[0].watchlist == "core"


def test_ma_proximity_silent_outside_threshold(rules):
    # Same shape, last two closes 101.5 instead: sma_50 == 100.06, sma_150 ==
    # 100.02, both ~1.4-1.5% away -- outside the 1.0% band. Same value on both
    # of the last two closes keeps price on the same side of every MA (no
    # cross); RSI is 76.35, below the 80 extreme-overbought bound.
    closes = oscillating(100.0, 148) + [101.5, 101.5]
    bars = bars_from_closes(closes)
    assert evaluate("GOOG", bars, rules, armed=[]) == []


def test_ma_proximity_skipped_when_level_non_positive(rules):
    # 50 closes flat at 0.0 -> sma_50 == 0.0, a non-positive level for which
    # _distance_pct returns None. Nothing here is impossible test data: a
    # Bar's close is just a float with no positivity constraint, so a data
    # gap or bad fixture producing zero/negative closes is reachable, unlike
    # range_break's prior high/low. previous_close == price == 0.0 keeps the
    # ma_cross branch from firing first, isolating the proximity check: it
    # must skip rather than construct a Trigger carrying a None distance.
    bars = flat_bars(0.0, 50)
    triggers = [t for t in evaluate("GOOG", bars, rules, armed=[]) if t.rule == "ma_proximity"]
    assert triggers == []


# --- ma_cross -------------------------------------------------------------


def test_ma_cross_fires_when_close_crosses_ma(rules):
    # 198 oscillating closes around 100.0 followed by 98.0 then 102.0: every
    # MA (50/150/200) sits at exactly 100.0. The previous close (98.0) is
    # below all three, the latest (102.0) is above all three -- a cross on
    # every period. Distance from each MA is 2.0%, outside the 1.0%
    # proximity band, and RSI is 65.28, well below the extreme bound.
    closes = oscillating(100.0, 198) + [98.0, 102.0]
    bars = bars_from_closes(closes)
    triggers = evaluate("GOOG", bars, rules, armed=[])
    assert [t.rule for t in triggers] == ["ma_cross", "ma_cross", "ma_cross"]
    assert all(t.level == pytest.approx(100.0, abs=1e-9) for t in triggers)
    assert all(t.price == 102.0 for t in triggers)


def test_ma_cross_silent_when_price_stays_on_same_side(rules):
    # Same baseline, last two closes 101.6 then 101.7: both above every MA
    # (100.066 / 100.022 / 100.0165), so no side-crossing. RSI is 77.95,
    # below the 80 extreme bound.
    closes = oscillating(100.0, 198) + [101.6, 101.7]
    bars = bars_from_closes(closes)
    assert evaluate("GOOG", bars, rules, armed=[]) == []


def test_ma_cross_skipped_when_level_non_positive(rules):
    # 49 closes flat at 0.0, then -1.0 (previous), then 1.0 (current):
    # sma_50 over the trailing 50 closes (49 zeros + -1.0) == -0.02, a
    # non-positive level for which _distance_pct returns None. previous_close
    # (-1.0) < ma (-0.02) <= price (1.0) satisfies the cross condition, so
    # this exercises the branch that used to construct a Trigger straight
    # from _distance_pct's result. Only 51 bars total, short of the 150/200
    # MA periods, so sma_50 is the only candidate. As with ma_proximity, a
    # zero/negative close is reachable fixture data, not contrived.
    closes = [0.0] * 49 + [-1.0, 1.0]
    bars = bars_from_closes(closes)
    triggers = [t for t in evaluate("GOOG", bars, rules, armed=[]) if t.rule == "ma_cross"]
    assert triggers == []


# --- range_break -----------------------------------------------------------
#
# range_break's level is `max(prior_highs)` or `min(prior_lows)` -- a prior
# high or low actually reached by price, not a derived average like an SMA.
# There is no fixture that makes that non-positive without every high/low in
# the trailing window also being non-positive, which is not a real market
# condition this system's data ever produces (Alpaca daily bars are always
# positive prices). Unlike ma_proximity/ma_cross (an SMA can land at or below
# zero from ordinary-looking zero/negative closes) and armed_level (caller
# data with no positivity guarantee at all), there is no honest non-impossible
# fixture for this rule -- so no skip test is added here; the guard added at
# engine/triggers.py's range_break call site exists for type coherence with
# the now-plain-`float` `Trigger.distance_pct`, not a reachable runtime path.


def test_range_break_fires_with_volume_confirmation(rules):
    # 20 oscillating baseline bars (max prior close/high 100.05) then a
    # breakout bar at 110.0 on 2,000,000 volume -- 2.0x the 20-session
    # average of 1,000,000, clearing the 1.5x confirmation threshold. Only 21
    # bars total, short of every MA period, so no MA rule can fire.
    closes = oscillating(100.0, 20) + [110.0]
    volumes = [1_000_000] * 20 + [2_000_000]
    bars = bars_from_closes(closes, volume=volumes)
    triggers = [t for t in evaluate("GOOG", bars, rules, armed=[]) if t.rule == "range_break"]
    assert len(triggers) == 1
    assert triggers[0].level == pytest.approx(100.05, abs=1e-9)
    assert triggers[0].price == 110.0
    assert triggers[0].volume_ratio == pytest.approx(2.0)


def test_range_break_silent_without_volume_confirmation(rules):
    # Same breakout, but volume is only 1.1x the baseline average -- below
    # the 1.5x confirmation threshold.
    closes = oscillating(100.0, 20) + [110.0]
    volumes = [1_000_000] * 20 + [1_100_000]
    bars = bars_from_closes(closes, volume=volumes)
    triggers = [t for t in evaluate("GOOG", bars, rules, armed=[]) if t.rule == "range_break"]
    assert triggers == []


# --- armed_level -------------------------------------------------------------


def test_armed_level_fires_when_crossed(rules):
    bars = flat_bars(100.0, 20)
    bars[-2] = Bar(t="2026-08-04T00:00:00Z", o=349.0, h=349.0, l=349.0, c=349.0, v=1_000_000)
    bars[-1] = Bar(t="2026-08-05T00:00:00Z", o=351.0, h=351.0, l=351.0, c=351.0, v=1_000_000)
    triggers = [
        t for t in evaluate("GOOG", bars, rules, armed=[350.0]) if t.rule == "armed_level"
    ]
    assert len(triggers) == 1
    assert triggers[0].level == 350.0
    assert triggers[0].price == 351.0


def test_armed_level_skipped_when_non_positive(rules):
    # Finding 4: `armed` is caller data (--armed CLI floats today, a DB value
    # later), not something computed here -- a non-positive level cannot
    # produce a meaningful distance. It must be skipped, not crash the whole
    # ticker with a ZeroDivisionError, matching engine/indicators.py's
    # philosophy of returning None over raising on a scheduled run.
    bars = flat_bars(100.0, 20)
    bars[-2] = Bar(t="2026-08-04T00:00:00Z", o=-1.0, h=-1.0, l=-1.0, c=-1.0, v=1_000_000)
    bars[-1] = Bar(t="2026-08-05T00:00:00Z", o=1.0, h=1.0, l=1.0, c=1.0, v=1_000_000)
    triggers = [
        t for t in evaluate("GOOG", bars, rules, armed=[0.0, -5.0]) if t.rule == "armed_level"
    ]
    assert triggers == []


def test_armed_level_silent_when_not_crossed(rules):
    bars = flat_bars(100.0, 20)
    bars[-2] = Bar(t="2026-08-04T00:00:00Z", o=340.0, h=340.0, l=340.0, c=340.0, v=1_000_000)
    bars[-1] = Bar(t="2026-08-05T00:00:00Z", o=345.0, h=345.0, l=345.0, c=345.0, v=1_000_000)
    triggers = [
        t for t in evaluate("GOOG", bars, rules, armed=[350.0]) if t.rule == "armed_level"
    ]
    assert triggers == []


# --- rsi_extreme -----------------------------------------------------------


def test_rsi_extreme_fires_at_overbought_bound(rules):
    # 20 strictly rising closes -> all gains, no losses -> RSI(14) == 100.0 by
    # the "no downside" branch already covered in test_indicators.py, well
    # past the configured extreme-overbought bound of 80. 20 bars is short of
    # every MA period and short of the 20 prior bars range_break needs before
    # the current one, so only rsi_extreme can fire.
    closes = [100.0 + i for i in range(20)]
    bars = bars_from_closes(closes)
    triggers = evaluate("GOOG", bars, rules, armed=[])
    assert [t.rule for t in triggers] == ["rsi_extreme"]
    assert triggers[0].rsi == 100.0
    assert triggers[0].level == 80


def test_rsi_extreme_silent_between_bounds(rules):
    # Flat closes -> RSI 50.0 (already covered in test_indicators.py),
    # comfortably between both the 30/70 and 20/80 bounds.
    bars = flat_bars(100.0, 20)
    assert evaluate("GOOG", bars, rules, armed=[]) == []


# --- bar_timestamp ----------------------------------------------------------


def test_trigger_carries_bar_timestamp_not_now(rules):
    bars = flat_bars(100.0, 20)
    bars[-2] = Bar(t="2026-08-04T00:00:00Z", o=349.0, h=349.0, l=349.0, c=349.0, v=1_000_000)
    bars[-1] = Bar(
        t="2026-08-05T14:15:00-04:00", o=351.0, h=351.0, l=351.0, c=351.0, v=1_000_000
    )
    triggers = evaluate("GOOG", bars, rules, armed=[350.0])
    armed_trigger = next(t for t in triggers if t.rule == "armed_level")
    assert armed_trigger.bar_timestamp == "2026-08-05T14:15:00-04:00"


# --- Bar / Rules construction -------------------------------------------------


def test_bar_from_api_maps_alpaca_keys():
    bar = Bar.from_api(
        {
            "t": "2026-08-05T00:00:00Z",
            "o": 1.0,
            "h": 2.0,
            "l": 0.5,
            "c": 1.5,
            "v": 100,
            "n": 5,
            "vw": 1.4,
        }
    )
    assert bar == Bar(t="2026-08-05T00:00:00Z", o=1.0, h=2.0, l=0.5, c=1.5, v=100)


def test_rules_from_mapping_reads_triggers_section(rules):
    assert rules.ma_periods == (50, 150, 200)
    assert rules.volume_multiple == 1.5
    assert rules.rsi_extreme_overbought == 80
    assert rules.rsi_extreme_oversold == 20
