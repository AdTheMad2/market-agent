"""Tests for engine.ranking.

Written before the implementation (workspace TDD rule for `engine/`).
Expected orderings are hand-computed from the sort key described in
docs/IMPLEMENTATION_PLAN.md Task 1.4, never captured from `engine.ranking.rank`
(the code under test): (1) armed_level before every other rule, (2)
not-demoted before demoted, (3) watchlist == "core" before anything else,
(4) urgency ascending -- 0.0 for breach rules (armed_level, ma_cross,
range_break, rsi_extreme), distance_pct for ma_proximity, since a price
percentage and an RSI-point percentage are not the same unit -- (5)
volume_ratio descending with None as the weakest value, (6) ticker
ascending, (7) rule ascending.

See docs/SPEC.md Section 6.3 and docs/IMPLEMENTATION_PLAN.md Task 1.4.
"""

from __future__ import annotations

import pytest

from engine.ranking import rank
from engine.suppressors import Suppressed
from engine.triggers import Trigger


def make_trigger(
    ticker: str = "AAPL",
    rule: str = "ma_proximity",
    distance_pct: float = 0.5,
    volume_ratio: float | None = 1.0,
    watchlist: str = "core",
) -> Trigger:
    return Trigger(
        ticker=ticker,
        rule=rule,
        level=100.0,
        price=100.5,
        distance_pct=distance_pct,
        volume_ratio=volume_ratio,
        rsi=55.0,
        bar_timestamp="2026-08-05T00:00:00Z",
        watchlist=watchlist,
    )


def make_suppressed(demoted: bool = False, reason: str | None = None, **trigger_kwargs) -> Suppressed:
    return Suppressed(trigger=make_trigger(**trigger_kwargs), demoted=demoted, reason=reason)


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def five_triggers() -> list[Suppressed]:
    return [
        make_suppressed(ticker="A", distance_pct=0.1),
        make_suppressed(ticker="B", distance_pct=0.2),
        make_suppressed(ticker="C", distance_pct=0.3),
        make_suppressed(ticker="D", distance_pct=0.4),
        make_suppressed(ticker="E", distance_pct=0.5),
    ]


@pytest.fixture
def three_triggers() -> list[Suppressed]:
    return [
        make_suppressed(ticker="A", distance_pct=0.1),
        make_suppressed(ticker="B", distance_pct=0.2),
        make_suppressed(ticker="C", distance_pct=0.3),
    ]


@pytest.fixture
def mixed_triggers() -> list[Suppressed]:
    return [
        # Best possible distance/volume, but not armed_level.
        make_suppressed(ticker="AAPL", rule="rsi_extreme", distance_pct=0.01, volume_ratio=9.0),
        make_suppressed(ticker="MSFT", rule="ma_proximity", distance_pct=0.02, volume_ratio=8.0),
        # Worst distance/volume of the set, but armed_level -> must still win.
        make_suppressed(ticker="ZZZ", rule="armed_level", distance_pct=9.9, volume_ratio=0.1),
    ]


# --- ceiling ---------------------------------------------------------------


def test_ceiling_is_absolute(five_triggers):
    to_send, dropped = rank(five_triggers, already_sent_today=0, ceiling=3)
    assert len(to_send) == 3
    assert len(dropped) == 2


def test_ceiling_accounts_for_alerts_already_sent(three_triggers):
    to_send, dropped = rank(three_triggers, already_sent_today=2, ceiling=3)
    assert len(to_send) == 1
    assert len(dropped) == 2


def test_ceiling_fully_consumed_by_already_sent_yields_empty_to_send(three_triggers):
    to_send, dropped = rank(three_triggers, already_sent_today=3, ceiling=3)
    assert to_send == []
    assert len(dropped) == 3


def test_over_budget_already_sent_yields_empty_to_send(three_triggers):
    to_send, dropped = rank(three_triggers, already_sent_today=10, ceiling=3)
    assert to_send == []
    assert len(dropped) == 3


def test_negative_already_sent_raises(three_triggers):
    # A negative already_sent_today can only arise from a caller bug in the
    # state layer (Phase 2/4); silently returning an empty to_send would make
    # that defect indistinguishable from a genuinely quiet market.
    with pytest.raises(ValueError, match="-1"):
        rank(three_triggers, already_sent_today=-1, ceiling=3)


def test_negative_ceiling_raises(three_triggers):
    with pytest.raises(ValueError, match="-1"):
        rank(three_triggers, already_sent_today=0, ceiling=-1)


# --- ordering ----------------------------------------------------------------


def test_armed_levels_outrank_everything(mixed_triggers):
    to_send, _ = rank(mixed_triggers, already_sent_today=0, ceiling=1)
    assert to_send[0].trigger.rule == "armed_level"


def test_demoted_sorts_last_within_group():
    demoted_first = make_suppressed(ticker="A", demoted=True, reason="earnings", distance_pct=0.1)
    not_demoted_second = make_suppressed(ticker="Z", demoted=False, distance_pct=9.9)
    to_send, dropped = rank([demoted_first, not_demoted_second], already_sent_today=0, ceiling=2)
    assert to_send == [not_demoted_second, demoted_first]
    assert dropped == []


def test_demoted_armed_level_still_outranks_clean_non_armed():
    # Ruling (Finding 3): armed_level-vs-not is ranked ahead of demoted-vs-not
    # in the sort key, so a demoted armed level still beats a clean,
    # never-demoted ma_proximity trigger. This is deliberate, not a bug: an
    # armed level was set by the user on purpose, so it stays reported --
    # carrying its demotion reason -- even when suppressed for the day.
    demoted_armed = make_suppressed(
        ticker="Z", rule="armed_level", demoted=True, reason="earnings", distance_pct=9.9
    )
    clean_proximity = make_suppressed(
        ticker="A", rule="ma_proximity", demoted=False, distance_pct=0.01
    )
    to_send, _ = rank([demoted_armed, clean_proximity], already_sent_today=0, ceiling=2)
    assert to_send == [demoted_armed, clean_proximity]


def test_core_watchlist_before_screened():
    screened = make_suppressed(ticker="A", watchlist="screened", distance_pct=0.1)
    core = make_suppressed(ticker="Z", watchlist="core", distance_pct=9.9)
    to_send, _ = rank([screened, core], already_sent_today=0, ceiling=2)
    assert to_send == [core, screened]


def test_distance_pct_ascending_within_same_group():
    far = make_suppressed(ticker="A", distance_pct=5.0)
    near = make_suppressed(ticker="B", distance_pct=0.1)
    to_send, _ = rank([far, near], already_sent_today=0, ceiling=2)
    assert to_send == [near, far]


def test_volume_ratio_descending_within_same_group():
    low_volume = make_suppressed(ticker="A", volume_ratio=0.5)
    high_volume = make_suppressed(ticker="B", volume_ratio=5.0)
    to_send, _ = rank([low_volume, high_volume], already_sent_today=0, ceiling=2)
    assert to_send == [high_volume, low_volume]


def test_missing_volume_ratio_never_outranks_a_real_one():
    missing = make_suppressed(ticker="A", volume_ratio=None)
    real = make_suppressed(ticker="B", volume_ratio=0.01)
    to_send, _ = rank([missing, real], already_sent_today=0, ceiling=2)
    assert to_send == [real, missing]


def test_rsi_extreme_not_buried_behind_price_triggers():
    # Finding 1: distance_pct is not commensurable across rule types -- it is
    # a price percentage for ma_proximity but an RSI-point percentage for
    # rsi_extreme, where a bar at RSI 95 (against an 80 extreme-overbought
    # bound) produces distance_pct == 18.75, dwarfing any realistic price
    # distance. rsi_extreme fires on a breach, so its urgency is 0.0 --
    # tied with the other breach rule here -- and both must outrank the
    # proximity rule despite its far smaller distance_pct.
    rsi_extreme_95 = make_suppressed(
        ticker="C", rule="rsi_extreme", distance_pct=18.75, volume_ratio=1.0
    )
    ma_cross_breach = make_suppressed(
        ticker="B", rule="ma_cross", distance_pct=0.05, volume_ratio=1.0
    )
    ma_proximity_near = make_suppressed(
        ticker="A", rule="ma_proximity", distance_pct=0.01, volume_ratio=1.0
    )
    to_send, _ = rank(
        [ma_proximity_near, ma_cross_breach, rsi_extreme_95], already_sent_today=0, ceiling=3
    )
    # Both breach rules (urgency 0.0) outrank the proximity rule (urgency
    # 0.01); between the two breach rules, ticker is the tiebreak.
    assert to_send == [ma_cross_breach, rsi_extreme_95, ma_proximity_near]


def test_final_tiebreak_is_ticker_then_rule():
    same_bbb = make_suppressed(ticker="BBB", rule="rsi_extreme")
    same_aaa = make_suppressed(ticker="AAA", rule="rsi_extreme")
    to_send, _ = rank([same_bbb, same_aaa], already_sent_today=0, ceiling=2)
    assert to_send == [same_aaa, same_bbb]


def test_sort_is_deterministic_regardless_of_input_order():
    a = make_suppressed(ticker="A", rule="ma_proximity")
    b = make_suppressed(ticker="A", rule="ma_cross")
    forward = rank([a, b], already_sent_today=0, ceiling=2)[0]
    backward = rank([b, a], already_sent_today=0, ceiling=2)[0]
    assert forward == backward == [b, a]  # "ma_cross" < "ma_proximity"


# --- conservation --------------------------------------------------------------


def test_conservation_no_item_lost_or_duplicated(five_triggers):
    to_send, dropped = rank(five_triggers, already_sent_today=0, ceiling=3)
    assert len(to_send) + len(dropped) == len(five_triggers)
    combined = to_send + dropped
    assert sorted(combined, key=id) == sorted(five_triggers, key=id)


def test_dropped_items_remain_in_sorted_order(five_triggers):
    to_send, dropped = rank(five_triggers, already_sent_today=0, ceiling=3)
    all_ranked = rank(five_triggers, already_sent_today=0, ceiling=len(five_triggers))[0]
    assert to_send == all_ranked[:3]
    assert dropped == all_ranked[3:]
