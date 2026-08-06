"""The intraday poll. The ceiling and the news guard are the properties that matter.

Two of these tests exist because the failure they catch is silent:

* `test_the_whole_run_is_inside_the_intraday_guard` — the news guard is armed by
  `sources.intraday_run()` and by nothing else, so a job that forgets the
  wrapper is unprotected and looks completely healthy
  (docs/IMPLEMENTATION_PLAN.md Task 4.1, constraint a).
* `test_one_minute_bars_never_reach_the_daily_engine` — RSI(14) over fifteen
  one-minute closes returns a plausible number, and a plausible number is
  exactly what nobody questions (constraint b).

The rest is the ceiling: three alerts a day, the fourth dropped and recorded so
the evening digest can report it.

See docs/SPEC.md §6.3 and docs/IMPLEMENTATION_PLAN.md Task 4.1.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import sources
from engine.triggers import Bar
from jobs import intraday
from sources import store

# 15:00 UTC on a Wednesday is 11:00 ET — mid-session under EDT.
MID_SESSION = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 8, 5, 20, 30, tzinfo=UTC)


def minute_bars(low: float, high: float, close: float | None = None) -> list[Bar]:
    close = high if close is None else close
    return [
        Bar(t=f"2026-08-05T14:5{i}:00Z", o=low, h=high, l=low, c=close, v=10_000)
        for i in range(3)
    ]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """One armed level, bars that touch it, delivery captured."""
    sent: list[str] = []

    watchlist_file = tmp_path / "watchlist_core.yml"
    watchlist_file.write_text("tickers:\n  - GOOGL\n  - NVDA\n", encoding="utf-8")
    monkeypatch.setattr(intraday.watchlist, "WATCHLIST_PATH", watchlist_file)

    monkeypatch.setattr(
        intraday.alpaca,
        "intraday_bars",
        lambda tickers, minutes=None: {t: minute_bars(349.0, 351.0, 350.6) for t in tickers},
    )

    def fake_send(text, dry_run=False, to_test_chat=False):
        sent.append(text)
        return True

    monkeypatch.setattr(intraday.telegram, "send", fake_send)

    db = tmp_path / "market.db"
    store.init_db(db)
    return {"db": db, "sent": sent, "watchlist": watchlist_file}


def arm(db, ticker: str, level: float, direction: str = "both") -> None:
    store.arm_level(db, ticker, level, armed_on=MID_SESSION.date(), direction=direction)


# --------------------------------------------------------------------------
# The guard — constraint (a)
# --------------------------------------------------------------------------


def test_the_whole_run_is_inside_the_intraday_guard(wired, monkeypatch):
    # The wrapper is what arms sources.forbid_intraday. Without it the job runs
    # perfectly and the guard it depends on is simply off.
    seen = []
    monkeypatch.setattr(
        intraday.alpaca,
        "intraday_bars",
        lambda tickers, minutes=None: (
            seen.append(sources.in_intraday_run()),
            {t: minute_bars(349.0, 351.0) for t in tickers},
        )[1],
    )
    arm(wired["db"], "GOOGL", 350.0)
    intraday.run(db=wired["db"], moment=MID_SESSION)
    assert seen == [True]


def test_a_news_fetch_from_inside_the_poll_raises(wired, monkeypatch):
    # The guard's whole purpose, asserted rather than assumed.
    arm(wired["db"], "GOOGL", 350.0)
    raised = []
    monkeypatch.setattr(
        intraday.alpaca,
        "intraday_bars",
        lambda tickers, minutes=None: (
            raised.append(_news_raises()),
            {t: minute_bars(349.0, 351.0) for t in tickers},
        )[1],
    )
    intraday.run(db=wired["db"], moment=MID_SESSION)
    assert raised == [True]


def _news_raises() -> bool:
    try:
        sources.forbid_intraday("company news")
    except RuntimeError:
        return True
    return False


# --------------------------------------------------------------------------
# Constraint (b)
# --------------------------------------------------------------------------


def test_one_minute_bars_never_reach_the_daily_engine(wired, monkeypatch):
    from engine import triggers

    monkeypatch.setattr(
        triggers,
        "evaluate",
        lambda *a, **k: pytest.fail("1-minute bars must not be evaluated as a daily series"),
    )
    arm(wired["db"], "GOOGL", 350.0)
    intraday.run(db=wired["db"], moment=MID_SESSION)


def test_an_alert_reports_no_rsi_or_volume(wired):
    # Both are undefined over a 15-minute window; the renderer prints n/a.
    arm(wired["db"], "GOOGL", 350.0)
    intraday.run(db=wired["db"], moment=MID_SESSION)
    assert "n/a" in wired["sent"][0]


# --------------------------------------------------------------------------
# The session gate
# --------------------------------------------------------------------------


def test_a_poll_outside_the_session_does_nothing(wired):
    arm(wired["db"], "GOOGL", 350.0)
    outcome = intraday.run(db=wired["db"], moment=AFTER_CLOSE)
    assert outcome.ran is False
    assert wired["sent"] == []


def test_no_armed_levels_means_no_fetch_at_all(wired, monkeypatch):
    monkeypatch.setattr(
        intraday.alpaca,
        "intraday_bars",
        lambda *a, **k: pytest.fail("nothing is armed; there is no question to ask"),
    )
    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.ran is True
    assert outcome.touched == 0


# --------------------------------------------------------------------------
# The ceiling — the Phase 4 observable check, in unit form
# --------------------------------------------------------------------------


def test_five_touches_send_three_and_drop_two(wired):
    for level in (349.2, 349.5, 350.0, 350.3, 350.5):
        arm(wired["db"], "GOOGL", level)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)

    assert outcome.touched == 5
    assert outcome.sent == 3
    assert outcome.dropped == 2
    assert len(wired["sent"]) == 3


def test_the_two_dropped_are_recorded_for_the_evening_digest(wired):
    for level in (349.2, 349.5, 350.0, 350.3, 350.5):
        arm(wired["db"], "GOOGL", level)
    intraday.run(db=wired["db"], moment=MID_SESSION)

    # The post-close job is a separate process; this table is the only way the
    # dropped alerts reach it.
    dropped = store.dropped_on(wired["db"], MID_SESSION.date())
    assert len(dropped) == 2
    assert all("ceiling" in row["reason"] for row in dropped)


def test_the_ceiling_counts_alerts_already_sent_earlier_today(wired):
    store.record_sent(
        wired["db"], "NVDA", "armed_level", level=100.0,
        bar_ts="2026-08-05T14:00:00Z", sent_at="2026-08-05T14:01:00Z",
    )
    store.record_sent(
        wired["db"], "NVDA", "ma_proximity", level=101.0,
        bar_ts="2026-08-05T14:00:00Z", sent_at="2026-08-05T14:02:00Z",
    )
    for level in (349.5, 350.0, 350.5):
        arm(wired["db"], "GOOGL", level)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.sent == 1
    assert outcome.dropped == 2


def test_a_digest_does_not_eat_the_intraday_ceiling(wired):
    store.record_sent(
        wired["db"], "_digest", "premarket", level=None,
        bar_ts="2026-08-05T00:00:00Z", sent_at="2026-08-05T13:16:00Z",
        kind=store.KIND_DIGEST,
    )
    for level in (349.5, 350.0, 350.5):
        arm(wired["db"], "GOOGL", level)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.sent == 3


# --------------------------------------------------------------------------
# Disarm and cooldown
# --------------------------------------------------------------------------


def test_a_level_is_disarmed_once_it_alerts(wired):
    arm(wired["db"], "GOOGL", 350.0)
    intraday.run(db=wired["db"], moment=MID_SESSION)
    assert store.armed_for(wired["db"], "GOOGL") == []


def test_a_failed_delivery_leaves_the_level_armed(wired, monkeypatch):
    monkeypatch.setattr(intraday.telegram, "send", lambda *a, **k: False)
    arm(wired["db"], "GOOGL", 350.0)
    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)

    assert outcome.sent == 0
    assert store.armed_for(wired["db"], "GOOGL") == [350.0]
    assert any(e.startswith("delivery:") for e in outcome.errors)


def test_a_dry_run_writes_no_state(wired):
    arm(wired["db"], "GOOGL", 350.0)
    intraday.run(db=wired["db"], moment=MID_SESSION, dry_run=True)

    assert store.armed_for(wired["db"], "GOOGL") == [350.0]
    assert store.sent_count_today(wired["db"], MID_SESSION.date()) == 0


def test_the_cooldown_is_per_level_not_per_ticker(wired):
    # A name with two armed levels must not go quiet on both because one fired.
    store.record_sent(
        wired["db"], "GOOGL", "armed_level", level=350.0,
        bar_ts="2026-08-05T14:30:00Z", sent_at="2026-08-05T14:45:00Z",
    )
    arm(wired["db"], "GOOGL", 350.0)
    arm(wired["db"], "GOOGL", 349.5)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.sent == 1
    assert store.armed_for(wired["db"], "GOOGL") == [350.0]


def test_a_re_armed_level_stays_quiet_inside_the_cooldown(wired):
    store.record_sent(
        wired["db"], "GOOGL", "armed_level", level=350.0,
        bar_ts="2026-08-05T14:30:00Z", sent_at="2026-08-05T14:45:00Z",
    )
    arm(wired["db"], "GOOGL", 350.0)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.sent == 0


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_a_ticker_returning_no_bars_costs_only_itself(wired, monkeypatch):
    monkeypatch.setattr(
        intraday.alpaca,
        "intraday_bars",
        lambda tickers, minutes=None: {"GOOGL": minute_bars(349.0, 351.0), "NVDA": []},
    )
    arm(wired["db"], "GOOGL", 350.0)
    arm(wired["db"], "NVDA", 350.0)

    outcome = intraday.run(db=wired["db"], moment=MID_SESSION)
    assert outcome.sent == 1
    assert any(e.startswith("bars: NVDA") for e in outcome.errors)
