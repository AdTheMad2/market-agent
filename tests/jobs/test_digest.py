"""The digest pipeline. Network and delivery are stubbed; ordering is not.

The properties asserted here are the ones that survive a bad day:

* a non-session day does no work and sends nothing
* state is written before the message is sent, so a delivery failure costs a
  message rather than the run's bars
* a dead news provider costs its section, not the digest
* the two digests are recorded as `kind="digest"` and never consume the
  intraday ceiling
* pre-market arms from the YAML; post-close disarms what fired

See docs/IMPLEMENTATION_PLAN.md Tasks 3.3 and 3.4.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.triggers import Bar
from jobs import digest
from sources import Earnings, MacroEvent, NewsItem, store

TRADING_DAY = date(2026, 8, 5)
HOLIDAY = date(2026, 8, 8)  # a Saturday


def make_bars(count: int, base: float = 100.0) -> list[Bar]:
    """Flat bars, then a final close 0.4% above them.

    Flat closes make every SMA equal to `base`, so the last bar sits inside
    `ma_proximity_pct` and exactly one family of rules fires — enough to prove
    the pipeline carries a trigger through without making the test a second
    copy of the engine's own tests.
    """
    # Consecutive calendar days ending on the trading day. Hand-rolled month
    # arithmetic collided with the final bar's date and silently cost a row.
    start = TRADING_DAY - timedelta(days=count - 1)
    bars = [
        Bar(
            t=f"{(start + timedelta(days=i)).isoformat()}T04:00:00Z",
            o=base,
            h=base,
            l=base,
            c=base,
            v=1_000_000,
        )
        for i in range(count - 1)
    ]
    last = base * 1.004
    bars.append(
        Bar(t=f"{TRADING_DAY.isoformat()}T04:00:00Z", o=last, h=last, l=last, c=last, v=1_000_000)
    )
    return bars


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Every network call stubbed, delivery captured, watchlist pinned."""
    sent: list[str] = []

    watchlist_file = tmp_path / "watchlist_core.yml"
    watchlist_file.write_text(
        "tickers:\n  - GOOGL\narmed_levels:\n"
        "  - ticker: GOOGL\n    level: 100.2\n    direction: both\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(digest.watchlist, "WATCHLIST_PATH", watchlist_file)

    monkeypatch.setattr(
        digest.alpaca, "daily_bars", lambda tickers, days: {"GOOGL": make_bars(250)}
    )
    monkeypatch.setattr(
        digest.finnhub,
        "company_news",
        lambda tickers, since: {
            "GOOGL": [
                NewsItem(
                    ticker="GOOGL",
                    headline="Alphabet opens a data centre",
                    source="Reuters",
                    url="https://example.com/a",
                    published_at="2026-08-05T12:00:00Z",
                )
            ]
        },
    )
    monkeypatch.setattr(
        digest.finnhub,
        "earnings_calendar",
        lambda days, tickers=None: [Earnings(ticker="GOOGL", date="2026-08-20", when="amc")],
    )
    monkeypatch.setattr(
        digest.fred,
        "macro_calendar",
        lambda days: [MacroEvent(date="2026-08-12", name="Consumer Price Index")],
    )
    monkeypatch.setattr(digest.edgar, "recent_filings", lambda tickers: {"GOOGL": []})

    def fake_send(text, dry_run=False, to_test_chat=False):
        sent.append(text)
        return True

    monkeypatch.setattr(digest.telegram, "send", fake_send)
    return {"db": tmp_path / "market.db", "sent": sent, "watchlist": watchlist_file}


# --------------------------------------------------------------------------
# The calendar gate
# --------------------------------------------------------------------------


def test_a_non_session_day_does_no_work_and_sends_nothing(wired):
    outcome = digest.run(digest.POSTCLOSE, db=wired["db"], day=HOLIDAY)
    assert outcome.ran is False
    assert wired["sent"] == []
    # Not merely "no message": no database was touched either.
    assert not wired["db"].exists()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_digest_is_delivered_and_carries_a_trigger(wired):
    outcome = digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert outcome.ran is True
    assert outcome.delivered is True
    assert outcome.triggers > 0
    assert "GOOGL" in wired["sent"][0]


def test_bars_are_persisted_before_the_message_is_sent(wired, monkeypatch):
    # Delivery failure must cost the message, not the run's data.
    monkeypatch.setattr(digest.telegram, "send", lambda *a, **k: False)
    outcome = digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert outcome.delivered is False
    assert store.bar_count(wired["db"], "GOOGL") == 250


def test_a_failed_delivery_records_no_sent_row(wired, monkeypatch):
    monkeypatch.setattr(digest.telegram, "send", lambda *a, **k: False)
    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert store.sent_count_today(wired["db"], TRADING_DAY, kind=store.KIND_DIGEST) == 0


def test_a_digest_never_consumes_the_intraday_ceiling(wired):
    digest.run(digest.PREMARKET, db=wired["db"], day=TRADING_DAY)
    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert store.sent_count_today(wired["db"], TRADING_DAY, kind=store.KIND_DIGEST) == 2
    assert store.sent_count_today(wired["db"], TRADING_DAY, kind=store.KIND_INTRADAY) == 0


def test_a_rerun_for_a_past_day_files_its_digest_under_that_day(wired):
    # `--day` exists so a missed run can be re-run for the day it missed.
    # Recording the row under today instead would make that day read as zero
    # digests sent, and the missed run would look missed forever.
    earlier = date(2026, 8, 4)
    digest.run(digest.POSTCLOSE, db=wired["db"], day=earlier)
    assert store.sent_count_today(wired["db"], earlier, kind=store.KIND_DIGEST) == 1


def test_a_dry_run_writes_no_sent_row(wired):
    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY, dry_run=True)
    assert store.sent_count_today(wired["db"], TRADING_DAY, kind=store.KIND_DIGEST) == 0


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_a_dead_news_provider_costs_its_section_not_the_digest(wired, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("finnhub returned HTTP 503")

    monkeypatch.setattr(digest.finnhub, "company_news", explode)
    outcome = digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert outcome.delivered is True
    assert any("news" in message for message in outcome.errors)


def test_every_context_provider_can_fail_at_once(wired, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("down")

    for module, name in (
        (digest.finnhub, "company_news"),
        (digest.finnhub, "earnings_calendar"),
        (digest.fred, "macro_calendar"),
        (digest.edgar, "recent_filings"),
    ):
        monkeypatch.setattr(module, name, explode)

    outcome = digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert outcome.delivered is True
    assert len(outcome.errors) == 4


# --------------------------------------------------------------------------
# Armed-level lifecycle
# --------------------------------------------------------------------------


def test_premarket_arms_the_levels_in_the_yaml(wired):
    digest.run(digest.PREMARKET, db=wired["db"], day=TRADING_DAY)
    assert store.armed_for(wired["db"], "GOOGL") == [100.2]


def test_postclose_does_not_arm(wired):
    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert store.armed_for(wired["db"], "GOOGL") == []


def test_postclose_disarms_a_level_that_fired_today(wired):
    digest.run(digest.PREMARKET, db=wired["db"], day=TRADING_DAY)
    store.record_sent(
        wired["db"],
        "GOOGL",
        "armed_level",
        level=100.2,
        bar_ts="2026-08-05T18:00:00Z",
        sent_at="2026-08-05T18:01:00Z",
    )
    assert store.armed_for(wired["db"], "GOOGL") == [100.2]

    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert store.armed_for(wired["db"], "GOOGL") == []


def test_premarket_rearms_a_level_still_listed_in_the_yaml(wired):
    # The documented lifecycle: removal is a YAML edit, never a table edit.
    digest.run(digest.PREMARKET, db=wired["db"], day=TRADING_DAY)
    store.disarm_level(wired["db"], "GOOGL", 100.2)
    assert store.armed_for(wired["db"], "GOOGL") == []

    digest.run(digest.PREMARKET, db=wired["db"], day=TRADING_DAY)
    assert store.armed_for(wired["db"], "GOOGL") == [100.2]


# --------------------------------------------------------------------------
# The intraday guard
# --------------------------------------------------------------------------


def test_the_digest_run_is_not_an_intraday_run(wired, monkeypatch):
    # The inverse of what Phase 4's intraday job must assert. If a digest ever
    # entered `sources.intraday_run()`, every news fetch here would raise and
    # the failure would look like four dead providers.
    from sources import in_intraday_run

    seen = []
    original = digest.finnhub.company_news
    monkeypatch.setattr(
        digest.finnhub,
        "company_news",
        lambda tickers, since: (seen.append(in_intraday_run()), original(tickers, since))[1],
    )
    digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
    assert seen == [False]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_an_unknown_kind_raises_rather_than_sending_something_untitled(wired):
    with pytest.raises(ValueError):
        digest.run("midday", db=wired["db"], day=TRADING_DAY)


def test_an_empty_watchlist_raises_rather_than_sending_an_empty_digest(wired):
    wired["watchlist"].write_text("tickers: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        digest.run(digest.POSTCLOSE, db=wired["db"], day=TRADING_DAY)
