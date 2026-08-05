"""SQLite state: bars, armed levels, and the record of what was sent.

Every write here is idempotent. That is the whole point of the module: GitHub
cron double-fires, a backfill gets re-run by hand, and a workflow retries after
a network blip. A duplicate bar is not a visible error — it is a silently wrong
moving average two weeks later.

The database file is committed (docs/SPEC.md §7) and the repository is public,
so nothing personal may ever be written: prices, price levels, and delivery
records only.

Schema lives in `data/schema.sql`, not in this file, so the tables can be read
without reading Python.

See docs/IMPLEMENTATION_PLAN.md Task 2.2.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from engine.triggers import Bar

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market.db"


def connect(db: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and rows as tuples."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _session(db: str | Path) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on failure, and always close.

    `with sqlite3.connect(...)` commits but never closes; on Windows an unclosed
    handle keeps the file locked, which breaks the very re-run this module
    exists to make safe.
    """
    conn = connect(db)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db: str | Path) -> None:
    """Apply `data/schema.sql`. Safe to run against an existing database —
    every statement in the schema is `IF NOT EXISTS`."""
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with _session(db) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_bars(db: str | Path, ticker: str, bars: Iterable[Bar]) -> int:
    """Insert or replace bars for one ticker. Returns the number of rows written.

    `ON CONFLICT DO UPDATE` rather than `INSERT OR IGNORE`: a bar can legitimately
    be revised after the close (Alpaca corrects volume), and ignoring the second
    write would keep the stale figure forever.
    """
    rows = [(ticker, b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]
    if not rows:
        return 0
    with _session(db) as conn:
        conn.executemany(
            """
            INSERT INTO bars (ticker, ts, o, h, l, c, v)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, ts) DO UPDATE SET
                o = excluded.o,
                h = excluded.h,
                l = excluded.l,
                c = excluded.c,
                v = excluded.v
            """,
            rows,
        )
    return len(rows)


def bars_for(db: str | Path, ticker: str, limit: int | None = None) -> list[Bar]:
    """Stored bars for one ticker, oldest-first — the ordering every engine
    function assumes. `limit` takes the newest N and still returns them
    oldest-first.
    """
    with _session(db) as conn:
        if limit is None:
            rows = conn.execute(
                "SELECT ts, o, h, l, c, v FROM bars WHERE ticker = ? ORDER BY ts ASC",
                (ticker,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, o, h, l, c, v FROM bars WHERE ticker = ? ORDER BY ts DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()[::-1]
    return [Bar(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=int(r[5])) for r in rows]


def tickers_with_bars(db: str | Path) -> list[str]:
    with _session(db) as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM bars ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def bar_count(db: str | Path, ticker: str) -> int:
    with _session(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bars WHERE ticker = ?", (ticker,)
        ).fetchone()[0]


def arm_level(db: str | Path, ticker: str, level: float, armed_on: date | None = None) -> None:
    """Arm a price level. Re-arming an already-armed or previously disarmed
    level updates it in place and makes it active — the user re-adding a level
    to `config/watchlist_core.yml` must mean "watch this", not "insert a
    duplicate" or "nothing happened".
    """
    day = (armed_on or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        conn.execute(
            """
            INSERT INTO armed_levels (ticker, level, armed_on, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (ticker, level) DO UPDATE SET
                armed_on = excluded.armed_on,
                active = 1
            """,
            (ticker, float(level), day),
        )


def disarm_level(db: str | Path, ticker: str, level: float) -> None:
    """Deactivate a level after it triggers. The row stays so the history of
    what was armed, and when, survives."""
    with _session(db) as conn:
        conn.execute(
            "UPDATE armed_levels SET active = 0 WHERE ticker = ? AND level = ?",
            (ticker, float(level)),
        )


def armed_for(db: str | Path, ticker: str) -> list[float]:
    """Active armed levels for one ticker, ascending."""
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT level FROM armed_levels WHERE ticker = ? AND active = 1 ORDER BY level ASC",
            (ticker,),
        ).fetchall()
    return [float(r[0]) for r in rows]


def record_sent(
    db: str | Path,
    ticker: str,
    rule: str,
    level: float | None,
    bar_ts: str,
    sent_at: str | None = None,
) -> None:
    """Record that an alert was delivered.

    Unique on `(ticker, rule, bar_ts)`: a retry after a partial failure must not
    consume a second slot of the intraday ceiling for a message the user only
    ever saw once.
    """
    stamp = sent_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _session(db) as conn:
        conn.execute(
            """
            INSERT INTO sent_alerts (ticker, rule, level, bar_ts, sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (ticker, rule, bar_ts) DO NOTHING
            """,
            (ticker, rule, None if level is None else float(level), bar_ts, stamp),
        )


def sent_count_today(db: str | Path, day: date | None = None) -> int:
    """How many alerts were delivered on `day` (UTC), for the intraday ceiling.

    `day` is explicit rather than read from the clock so a job can pass the
    trading day it is actually running for, and so this is testable without
    freezing time.
    """
    target = (day or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM sent_alerts WHERE substr(sent_at, 1, 10) = ?",
            (target,),
        ).fetchone()[0]


def last_sent(db: str | Path, ticker: str, rule: str) -> str | None:
    """`sent_at` of the most recent delivery for this ticker+rule, for the
    same-trigger cooldown (`alerts.same_trigger_cooldown_minutes`)."""
    with _session(db) as conn:
        row = conn.execute(
            "SELECT sent_at FROM sent_alerts WHERE ticker = ? AND rule = ? "
            "ORDER BY sent_at DESC LIMIT 1",
            (ticker, rule),
        ).fetchone()
    return None if row is None else row[0]


def upsert_many(db: str | Path, bars_by_ticker: dict[str, Sequence[Bar]]) -> int:
    """Convenience for the backfill and the jobs: upsert every ticker's bars,
    returning the total rows written."""
    return sum(upsert_bars(db, ticker, bars) for ticker, bars in bars_by_ticker.items())
