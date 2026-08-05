"""SQLite state. The property that matters is idempotency.

GitHub cron can double-fire and a backfill can be re-run by hand. A write path
that duplicates rows corrupts the history silently — every indicator downstream
would then be computed over a series that never traded.

See docs/IMPLEMENTATION_PLAN.md Task 2.2.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from engine.triggers import Bar
from sources import store


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "market.db"
    store.init_db(path)
    return path


def make_bars(n: int, ticker_close: float = 100.0) -> list[Bar]:
    return [
        Bar(
            t=f"2026-07-{day:02d}T04:00:00Z",
            o=ticker_close,
            h=ticker_close + 1,
            l=ticker_close - 1,
            c=ticker_close + day,
            v=1_000_000 + day,
        )
        for day in range(1, n + 1)
    ]


def row_count(db, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "market.db"
    store.init_db(path)
    store.init_db(path)

    assert row_count(path, "bars") == 0


def test_upsert_bars_is_idempotent(tmp_db):
    ten_bars = make_bars(10)
    store.upsert_bars(tmp_db, "GOOG", ten_bars)
    store.upsert_bars(tmp_db, "GOOG", ten_bars)

    assert row_count(tmp_db, "bars") == 10


def test_upsert_bars_updates_a_revised_bar_in_place(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(3))
    revised = Bar(t="2026-07-03T04:00:00Z", o=1, h=2, l=0.5, c=999.0, v=42)
    store.upsert_bars(tmp_db, "GOOG", [revised])

    assert row_count(tmp_db, "bars") == 3
    assert store.bars_for(tmp_db, "GOOG")[-1].c == 999.0


def test_bars_for_returns_bars_oldest_first_for_one_ticker_only(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(3))
    store.upsert_bars(tmp_db, "NVDA", make_bars(2))

    goog = store.bars_for(tmp_db, "GOOG")
    assert [b.t for b in goog] == sorted(b.t for b in goog)
    assert len(goog) == 3
    assert len(store.bars_for(tmp_db, "NVDA")) == 2


def test_bars_for_honours_a_limit_by_taking_the_newest(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(10))

    bars = store.bars_for(tmp_db, "GOOG", limit=2)
    assert [b.t for b in bars] == ["2026-07-09T04:00:00Z", "2026-07-10T04:00:00Z"]


def test_bars_for_an_unknown_ticker_is_empty(tmp_db):
    assert store.bars_for(tmp_db, "NOPE") == []


def test_upsert_bars_with_no_bars_is_a_no_op(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", [])

    assert row_count(tmp_db, "bars") == 0


def test_armed_for_returns_only_active_levels_of_that_ticker(tmp_db):
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1))
    store.arm_level(tmp_db, "GOOG", 300.0, armed_on=date(2026, 8, 1))
    store.arm_level(tmp_db, "NVDA", 200.0, armed_on=date(2026, 8, 1))
    store.disarm_level(tmp_db, "GOOG", 300.0)

    assert store.armed_for(tmp_db, "GOOG") == [350.0]


def test_arm_level_is_idempotent(tmp_db):
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1))
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 2))

    assert row_count(tmp_db, "armed_levels") == 1
    assert store.armed_for(tmp_db, "GOOG") == [350.0]


def test_rearming_a_disarmed_level_makes_it_active_again(tmp_db):
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1))
    store.disarm_level(tmp_db, "GOOG", 350.0)
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 2))

    assert store.armed_for(tmp_db, "GOOG") == [350.0]


def test_record_sent_then_sent_count_today_counts_only_that_day(tmp_db):
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z",
    )
    store.record_sent(
        tmp_db, "NVDA", "ma_cross", None,
        bar_ts="2026-08-06T14:00:00Z", sent_at="2026-08-06T14:05:00Z",
    )

    assert store.sent_count_today(tmp_db, day=date(2026, 8, 6)) == 1
    assert store.sent_count_today(tmp_db, day=date(2026, 8, 5)) == 1
    assert store.sent_count_today(tmp_db, day=date(2026, 8, 7)) == 0


def test_record_sent_is_idempotent_per_ticker_rule_and_bar(tmp_db):
    for _ in range(2):
        store.record_sent(
            tmp_db, "GOOG", "armed_level", 350.0,
            bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z",
        )

    assert row_count(tmp_db, "sent_alerts") == 1
    assert store.sent_count_today(tmp_db, day=date(2026, 8, 5)) == 1


def test_the_same_rule_on_a_later_bar_is_a_new_alert(tmp_db):
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z",
    )
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T19:00:00Z", sent_at="2026-08-05T19:05:00Z",
    )

    assert store.sent_count_today(tmp_db, day=date(2026, 8, 5)) == 2


def test_last_sent_returns_the_most_recent_send_for_a_ticker_and_rule(tmp_db):
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z",
    )
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T19:00:00Z", sent_at="2026-08-05T19:05:00Z",
    )

    assert store.last_sent(tmp_db, "GOOG", "armed_level") == "2026-08-05T19:05:00Z"
    assert store.last_sent(tmp_db, "GOOG", "ma_cross") is None
