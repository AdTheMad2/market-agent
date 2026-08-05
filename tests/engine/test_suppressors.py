"""Tests for engine.suppressors.

Written before the implementation (workspace TDD rule for `engine/`).
`rules` loads the real config/rules.yml so these tests break the moment a
threshold key is renamed or removed from that file. Window boundaries are
computed by hand from the configured `*_within_days` values (7 / 3 / 1) and
a fixed `today` of 2026-08-05, never captured from `engine.suppressors.apply`
(the code under test).

The critical property under test is the asymmetry named in the task brief:
a suppressor may demote a trigger but has no code path that appends to or
removes from the list.

See docs/SPEC.md Section 5.3 and docs/IMPLEMENTATION_PLAN.md Task 1.3.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from engine.suppressors import Suppressed, SuppressionContext, SuppressorRules, apply
from engine.triggers import Trigger

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yml"

TODAY = date(2026, 8, 5)


@pytest.fixture(scope="module")
def suppressor_rules() -> SuppressorRules:
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return SuppressorRules.from_mapping(data)


def make_trigger(ticker: str = "GOOG") -> Trigger:
    return Trigger(
        ticker=ticker,
        rule="ma_proximity",
        level=100.0,
        price=100.4,
        distance_pct=0.4,
        volume_ratio=1.2,
        rsi=55.0,
        bar_timestamp="2026-08-05T00:00:00Z",
        watchlist="core",
    )


def make_context(
    suppressor_rules: SuppressorRules,
    *,
    earnings_dates: dict[str, date] | None = None,
    ex_dividend_dates: dict[str, date] | None = None,
    macro_events: list[date] | None = None,
    today: date = TODAY,
) -> SuppressionContext:
    return SuppressionContext(
        rules=suppressor_rules,
        earnings_dates=earnings_dates or {},
        ex_dividend_dates=ex_dividend_dates or {},
        macro_events=macro_events or [],
        today=today,
    )


# --- asymmetry ---------------------------------------------------------------


def test_suppressor_never_creates_a_trigger(suppressor_rules):
    context = make_context(suppressor_rules)
    assert apply([], context) == []


def test_apply_preserves_order_and_length(suppressor_rules):
    goog = make_trigger("GOOG")
    aapl = make_trigger("AAPL")
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 8, 6)})
    result = apply([goog, aapl], context)
    assert len(result) == 2
    assert result[0].trigger == goog
    assert result[1].trigger == aapl
    assert result[0].demoted is True
    assert result[1].demoted is False


def test_not_demoted_when_no_calendar_context_applies(suppressor_rules):
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules)
    result = apply([goog_trigger], context)
    assert result == [Suppressed(trigger=goog_trigger, demoted=False, reason=None)]


# --- earnings ------------------------------------------------------------------


def test_earnings_within_window_demotes_but_does_not_remove(suppressor_rules):
    goog_trigger = make_trigger("GOOG")
    # earnings_within_days == 7; today + 3 days is well inside the window.
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 8, 8)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is True
    assert "earnings" in result[0].reason
    assert result[0].trigger == goog_trigger  # still present, still visible


def test_earnings_on_forward_boundary_demotes(suppressor_rules):
    # today + 7 days == earnings_within_days exactly -> inclusive, demotes.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 8, 12)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is True
    assert result[0].reason == "earnings 2026-08-12"


def test_earnings_one_day_past_forward_boundary_not_demoted(suppressor_rules):
    # today + 8 days -> one day outside the 7-day window.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 8, 13)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is False
    assert result[0].reason is None


def test_earnings_on_backward_boundary_demotes(suppressor_rules):
    # today - 7 days == earnings_within_days exactly -> inclusive, demotes.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 7, 29)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is True
    assert result[0].reason == "earnings 2026-07-29"


def test_earnings_one_day_past_backward_boundary_not_demoted(suppressor_rules):
    # today - 8 days -> one day outside the 7-day window.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, earnings_dates={"GOOG": date(2026, 7, 28)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is False


def test_earnings_only_applies_to_matching_ticker(suppressor_rules):
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, earnings_dates={"AAPL": date(2026, 8, 6)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is False


# --- ex-dividend -----------------------------------------------------------------


def test_ex_dividend_on_forward_boundary_demotes(suppressor_rules):
    # ex_dividend_within_days == 3; today + 3 days is inclusive.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, ex_dividend_dates={"GOOG": date(2026, 8, 8)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is True
    assert result[0].reason == "ex-dividend 2026-08-08"


def test_ex_dividend_one_day_past_forward_boundary_not_demoted(suppressor_rules):
    # today + 4 days -> one day outside the 3-day window.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, ex_dividend_dates={"GOOG": date(2026, 8, 9)})
    result = apply([goog_trigger], context)
    assert result[0].demoted is False


# --- macro -----------------------------------------------------------------------


def test_macro_event_within_window_demotes_every_ticker(suppressor_rules):
    # macro_event_within_days == 1; today + 1 day is inclusive and applies to
    # every trigger, not just one ticker.
    goog_trigger = make_trigger("GOOG")
    aapl_trigger = make_trigger("AAPL")
    context = make_context(suppressor_rules, macro_events=[date(2026, 8, 6)])
    result = apply([goog_trigger, aapl_trigger], context)
    assert result[0].demoted is True
    assert result[0].reason == "macro 2026-08-06"
    assert result[1].demoted is True
    assert result[1].reason == "macro 2026-08-06"


def test_macro_event_two_days_out_not_demoted(suppressor_rules):
    # today + 2 days -> one day outside the 1-day window.
    goog_trigger = make_trigger("GOOG")
    context = make_context(suppressor_rules, macro_events=[date(2026, 8, 7)])
    result = apply([goog_trigger], context)
    assert result[0].demoted is False


# --- multiple causes ---------------------------------------------------------------


def test_multiple_causes_join_in_stable_sorted_order(suppressor_rules):
    goog_trigger = make_trigger("GOOG")
    context = make_context(
        suppressor_rules,
        earnings_dates={"GOOG": date(2026, 8, 8)},
        ex_dividend_dates={"GOOG": date(2026, 8, 8)},
        macro_events=[date(2026, 8, 6)],
    )
    result = apply([goog_trigger], context)
    assert result[0].demoted is True
    assert result[0].reason == "earnings 2026-08-08; ex-dividend 2026-08-08; macro 2026-08-06"


# --- SuppressorRules construction -------------------------------------------------


def test_suppressor_rules_from_mapping_reads_suppressors_section(suppressor_rules):
    assert suppressor_rules.earnings_within_days == 7
    assert suppressor_rules.ex_dividend_within_days == 3
    assert suppressor_rules.macro_event_within_days == 1
