"""SQLite state. The property that matters is idempotency.

GitHub cron can double-fire and a backfill can be re-run by hand. A write path
that duplicates rows corrupts the history silently — every indicator downstream
would then be computed over a series that never traded. The other half of that
same failure is a write that should update and quietly does not, so the tests
below cover both directions.

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


def make_bars(n: int, base: float = 100.0) -> list[Bar]:
    return [
        Bar(
            t=f"2026-07-{day:02d}T04:00:00Z",
            o=base,
            h=base + 1,
            l=base - 1,
            c=base + day,
            v=1_000_000 + day,
        )
        for day in range(1, n + 1)
    ]


def row_count(db, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------


def test_init_db_preserves_existing_rows(tmp_path):
    path = tmp_path / "market.db"
    store.init_db(path)
    store.upsert_bars(path, "GOOG", make_bars(3))

    store.init_db(path)

    # A schema re-apply that dropped and recreated the tables would leave 0.
    assert row_count(path, "bars") == 3


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


def test_intraday_bars_do_not_contaminate_the_daily_series(tmp_db):
    """The silent corruption the timeframe key exists to prevent: minute bars
    share a (ticker, ts) keyspace with daily bars and would simply interleave."""
    store.upsert_bars(tmp_db, "GOOG", make_bars(3), timeframe=store.DAILY)
    minute = [Bar(t="2026-07-02T14:31:00Z", o=1, h=2, l=0.5, c=1.5, v=10)]
    store.upsert_bars(tmp_db, "GOOG", minute, timeframe=store.INTRADAY)

    daily = store.bars_for(tmp_db, "GOOG", timeframe=store.DAILY)
    assert len(daily) == 3
    assert all(b.t.endswith("T04:00:00Z") for b in daily)
    assert len(store.bars_for(tmp_db, "GOOG", timeframe=store.INTRADAY)) == 1
    assert store.bar_count(tmp_db, "GOOG") == 3


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


def test_upsert_many_writes_every_ticker_and_reports_the_total(tmp_db):
    written = store.upsert_many(tmp_db, {"GOOG": make_bars(3), "NVDA": make_bars(2)})

    assert written == 5
    assert store.tickers_with_bars(tmp_db) == ["GOOG", "NVDA"]
    assert store.bar_count(tmp_db, "GOOG") == 3


def test_tickers_with_bars_is_scoped_to_the_timeframe(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(1), timeframe=store.DAILY)
    store.upsert_bars(tmp_db, "NVDA", make_bars(1), timeframe=store.INTRADAY)

    assert store.tickers_with_bars(tmp_db) == ["GOOG"]
    assert store.tickers_with_bars(tmp_db, timeframe=store.INTRADAY) == ["NVDA"]


def test_prune_bars_keeps_the_newest_per_ticker(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(10))
    store.upsert_bars(tmp_db, "NVDA", make_bars(4))

    deleted = store.prune_bars(tmp_db, keep=3)

    assert deleted == 8
    assert [b.t for b in store.bars_for(tmp_db, "GOOG")] == [
        "2026-07-08T04:00:00Z",
        "2026-07-09T04:00:00Z",
        "2026-07-10T04:00:00Z",
    ]
    assert store.bar_count(tmp_db, "NVDA") == 3


def test_prune_bars_is_a_no_op_when_nothing_exceeds_the_limit(tmp_db):
    store.upsert_bars(tmp_db, "GOOG", make_bars(3))

    assert store.prune_bars(tmp_db, keep=250) == 0
    assert store.bar_count(tmp_db, "GOOG") == 3


def test_prune_bars_rejects_a_non_positive_limit(tmp_db):
    with pytest.raises(ValueError):
        store.prune_bars(tmp_db, keep=0)


# --------------------------------------------------------------------------
# Armed levels
# --------------------------------------------------------------------------


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


def test_a_level_recomputed_with_float_drift_is_the_same_level(tmp_db):
    """0.1 + 0.2 != 0.3 in binary floating point. A REAL primary key would
    insert a second row here, and the disarm below would match neither."""
    store.arm_level(tmp_db, "GOOG", 350.3, armed_on=date(2026, 8, 1))
    store.arm_level(tmp_db, "GOOG", 350.1 + 0.2, armed_on=date(2026, 8, 1))

    assert row_count(tmp_db, "armed_levels") == 1
    assert store.disarm_level(tmp_db, "GOOG", 350.1 + 0.2) == 1
    assert store.armed_for(tmp_db, "GOOG") == []


def test_disarm_level_reports_when_it_matched_nothing(tmp_db):
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1))

    assert store.disarm_level(tmp_db, "GOOG", 999.0) == 0
    assert store.disarm_level(tmp_db, "GOOG", 350.0) == 1
    assert store.armed_for(tmp_db, "GOOG") == []


def test_rearming_a_disarmed_level_makes_it_active_again(tmp_db):
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1))
    store.disarm_level(tmp_db, "GOOG", 350.0)
    store.arm_level(tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 2))

    assert store.armed_for(tmp_db, "GOOG") == [350.0]


def test_armed_details_carries_direction_and_provenance(tmp_db):
    store.arm_level(
        tmp_db, "GOOG", 350.0, armed_on=date(2026, 8, 1), direction="above", source="manual"
    )
    store.arm_level(
        tmp_db, "GOOG", 300.0, armed_on=date(2026, 8, 1), direction="below", source="job"
    )

    details = store.armed_details(tmp_db, "GOOG")
    assert [d["level"] for d in details] == [300.0, 350.0]
    assert [d["source"] for d in details] == ["job", "manual"]
    assert [d["direction"] for d in details] == ["below", "above"]


def test_arm_level_rejects_an_unknown_direction(tmp_db):
    with pytest.raises(ValueError):
        store.arm_level(tmp_db, "GOOG", 350.0, direction="sideways")


# --------------------------------------------------------------------------
# The sent ledger
# --------------------------------------------------------------------------


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


def test_a_digest_does_not_consume_the_intraday_ceiling(tmp_db):
    """SPEC §6.3 is two budgets, not one: two digests plus at most three
    intraday alerts. An undifferentiated count would start the session at 2."""
    store.record_sent(
        tmp_db, "ALL", "digest", None,
        bar_ts="2026-08-05T13:00:00Z", sent_at="2026-08-05T13:15:00Z",
        kind=store.KIND_DIGEST,
    )
    store.record_sent(
        tmp_db, "GOOG", "armed_level", 350.0,
        bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z",
    )

    assert store.sent_count_today(tmp_db, day=date(2026, 8, 5)) == 1
    assert store.sent_count_today(tmp_db, day=date(2026, 8, 5), kind=store.KIND_DIGEST) == 1


def test_record_sent_rejects_an_unknown_kind(tmp_db):
    with pytest.raises(ValueError):
        store.record_sent(
            tmp_db, "GOOG", "armed_level", 350.0,
            bar_ts="2026-08-05T18:00:00Z", sent_at="2026-08-05T18:05:00Z", kind="sms",
        )


@pytest.mark.parametrize(
    "sent_at",
    ["2026-08-05T14:15:00-04:00", "2026-08-05 18:05:00", "2026-08-05", "not a time"],
)
def test_a_timestamp_the_day_match_cannot_read_is_rejected_on_the_way_in(tmp_db, sent_at):
    """`sent_count_today` matches a date prefix and `last_sent` sorts
    lexicographically; an offset form silently breaks both."""
    with pytest.raises(ValueError):
        store.record_sent(
            tmp_db, "GOOG", "armed_level", 350.0,
            bar_ts="2026-08-05T18:00:00Z", sent_at=sent_at,
        )


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


# --------------------------------------------------------------------------
# The dropped ledger
# --------------------------------------------------------------------------


def test_a_dropped_trigger_survives_for_the_evening_digest(tmp_db):
    """The intraday job and the post-close job are separate cron processes, so
    `rank`'s in-process `dropped` list cannot reach the digest on its own."""
    store.record_dropped(
        tmp_db, "NVDA", "rsi_extreme", "ceiling reached",
        bar_ts="2026-08-05T18:00:00Z", dropped_at="2026-08-05T18:05:00Z",
    )

    dropped = store.dropped_on(tmp_db, day=date(2026, 8, 5))
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "ceiling reached"
    assert store.dropped_on(tmp_db, day=date(2026, 8, 6)) == []


def test_record_dropped_is_idempotent_per_ticker_rule_and_bar(tmp_db):
    for _ in range(2):
        store.record_dropped(
            tmp_db, "NVDA", "rsi_extreme", "ceiling reached",
            bar_ts="2026-08-05T18:00:00Z", dropped_at="2026-08-05T18:05:00Z",
        )

    assert row_count(tmp_db, "dropped_alerts") == 1


# --------------------------------------------------------------------------
# level_key in the alert keys — found by the Phase 4 intraday job
# --------------------------------------------------------------------------


def test_three_levels_on_one_ticker_in_one_bar_record_three_rows(tmp_db):
    """The defect the intraday ceiling depends on not having.

    The original constraint was (ticker, rule, bar_ts). Three armed levels
    touched inside the same 1-minute bar share all three, so two of the three
    sends recorded nothing — and a ceiling counting one alert where three went
    out delivers a fourth instead of dropping it.
    """
    for level in (349.5, 350.0, 350.5):
        store.record_sent(
            tmp_db, "GOOGL", "armed_level", level=level,
            bar_ts="2026-08-05T15:00:00Z", sent_at="2026-08-05T15:01:00Z",
        )
    assert row_count(tmp_db, "sent_alerts") == 3
    assert store.sent_count_today(tmp_db, date(2026, 8, 5)) == 3


def test_the_same_level_in_the_same_bar_still_records_once(tmp_db):
    # The idempotency the constraint existed for in the first place: a retry
    # after a partial failure must not consume a second ceiling slot.
    for _ in range(3):
        store.record_sent(
            tmp_db, "GOOGL", "armed_level", level=350.0,
            bar_ts="2026-08-05T15:00:00Z", sent_at="2026-08-05T15:01:00Z",
        )
    assert row_count(tmp_db, "sent_alerts") == 1


def test_two_levels_dropped_in_one_bar_are_both_reported(tmp_db):
    for level in (350.3, 350.5):
        store.record_dropped(
            tmp_db, "GOOGL", "armed_level", reason="daily ceiling of 3 reached",
            bar_ts="2026-08-05T15:00:00Z", level=level,
            dropped_at="2026-08-05T15:01:00Z",
        )
    assert len(store.dropped_on(tmp_db, date(2026, 8, 5))) == 2


def test_the_same_level_held_back_all_afternoon_is_reported_once(tmp_db):
    """The digest reports what was held back, not how often it was re-checked.

    A level that qualifies once qualifies on every subsequent poll of the same
    session, and each poll carries a fresh `bar_ts` — so the row-per-bar ledger
    is correct and the digest's view of it is not. Five polls of one held-back
    level is one thing to tell the user about, and the observable check for the
    ceiling ("the other 2 appear in that evening's digest") reads as a failure
    at any other number.
    """
    for hour, bar_minute in ((14, 24), (15, 27), (16, 30), (17, 32), (18, 46)):
        store.record_dropped(
            tmp_db, "NVDA", "armed_level", reason="daily ceiling of 3 reached",
            bar_ts=f"2026-08-05T{hour:02d}:{bar_minute:02d}:00Z", level=214.0,
            dropped_at=f"2026-08-05T{hour + 1:02d}:00:00Z",
        )

    assert row_count(tmp_db, "dropped_alerts") == 5

    dropped = store.dropped_on(tmp_db, date(2026, 8, 5))
    assert len(dropped) == 1
    # The first time it was held back — the moment the ceiling actually bit.
    assert dropped[0]["bar_ts"] == "2026-08-05T14:24:00Z"
    assert dropped[0]["dropped_at"] == "2026-08-05T15:00:00Z"


def test_two_levels_held_back_across_many_polls_stay_two(tmp_db):
    for hour in (14, 15, 16):
        for level in (214.0, 216.0):
            store.record_dropped(
                tmp_db, "NVDA", "armed_level", reason="daily ceiling of 3 reached",
                bar_ts=f"2026-08-05T{hour:02d}:24:00Z", level=level,
                dropped_at=f"2026-08-05T{hour:02d}:54:00Z",
            )

    dropped = store.dropped_on(tmp_db, date(2026, 8, 5))
    assert [(r["ticker"], r["level"]) for r in dropped] == [
        ("NVDA", 214.0),
        ("NVDA", 216.0),
    ]


def test_digests_still_dedupe_without_a_level(tmp_db):
    # level is NULL for a digest, and SQL NULLs never compare equal — which is
    # why the key stores '' rather than leaving the column nullable inside it.
    for _ in range(2):
        store.record_sent(
            tmp_db, "_digest", "postclose", level=None,
            bar_ts="2026-08-05T00:00:00Z", sent_at="2026-08-05T21:16:00Z",
            kind=store.KIND_DIGEST,
        )
    assert row_count(tmp_db, "sent_alerts") == 1


def test_last_sent_narrows_to_one_level(tmp_db):
    store.record_sent(
        tmp_db, "GOOGL", "armed_level", level=350.0,
        bar_ts="2026-08-05T15:00:00Z", sent_at="2026-08-05T15:01:00Z",
    )
    assert store.last_sent(tmp_db, "GOOGL", "armed_level", level=350.0) is not None
    # A different armed level on the same ticker has not alerted, and must not
    # inherit the first one's cooldown.
    assert store.last_sent(tmp_db, "GOOGL", "armed_level", level=349.5) is None


def test_init_db_migrates_a_database_written_before_level_key(tmp_path):
    """The database is committed to the repository, so the one that matters
    already exists. A schema-only fix would leave it on the old definition."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sent_alerts (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker  TEXT NOT NULL,
                rule    TEXT NOT NULL,
                kind    TEXT NOT NULL DEFAULT 'intraday',
                level   REAL,
                bar_ts  TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE (ticker, rule, bar_ts)
            );
            CREATE INDEX idx_sent_alerts_sent_at ON sent_alerts (sent_at);
            INSERT INTO sent_alerts (ticker, rule, kind, level, bar_ts, sent_at)
            VALUES ('GOOGL', 'armed_level', 'intraday', 350.0,
                    '2026-08-05T15:00:00Z', '2026-08-05T15:01:00Z');
            """
        )

    store.init_db(path)

    with sqlite3.connect(path) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sent_alerts)")]
        rows = conn.execute("SELECT ticker, level, level_key FROM sent_alerts").fetchall()

    assert "level_key" in columns
    # The existing row survives and gets a key derived from its own level.
    assert rows == [("GOOGL", 350.0, "350.0000")]
    # And the constraint it was migrated for now holds.
    store.record_sent(
        path, "GOOGL", "armed_level", level=349.5,
        bar_ts="2026-08-05T15:00:00Z", sent_at="2026-08-05T15:01:00Z",
    )
    assert row_count(path, "sent_alerts") == 2


def test_init_db_migration_is_idempotent(tmp_db):
    store.record_sent(
        tmp_db, "GOOGL", "armed_level", level=350.0,
        bar_ts="2026-08-05T15:00:00Z", sent_at="2026-08-05T15:01:00Z",
    )
    store.init_db(tmp_db)
    store.init_db(tmp_db)
    assert row_count(tmp_db, "sent_alerts") == 1
