"""SQLite state: bars, armed levels, and the record of what was sent and dropped.

Every write here is idempotent. That is the whole point of the module: GitHub
cron double-fires, a backfill gets re-run by hand, and a workflow retries after
a network blip. A duplicate bar is not a visible error — it is a silently wrong
moving average two weeks later.

Three shapes exist to make a silent error loud instead:

* `upsert_bars` takes the **timeframe explicitly**. Daily and 1-minute bars do
  not collide on `(ticker, ts)`, so an intraday write into a daily series would
  interleave undetected.
* armed levels are keyed by a **fixed-precision string**, not a REAL, and
  `disarm_level` returns the row count so a miss can be checked rather than
  assumed.
* timestamps are **validated at the boundary**. `sent_count_today` matches on a
  date prefix and `last_sent` orders lexicographically; both are correct only
  for `%Y-%m-%dT%H:%M:%SZ`, so anything else is rejected on the way in.

The database file is committed (docs/SPEC.md §7) and the repository is public,
so nothing personal may ever be written: prices, price levels, and delivery
records only.

Schema lives in `data/schema.sql`, not in this file, so the tables can be read
without reading Python.

See docs/IMPLEMENTATION_PLAN.md Task 2.2.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from engine.triggers import Bar

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market.db"

DAILY = "1Day"
INTRADAY = "1Min"

KIND_INTRADAY = "intraday"
KIND_DIGEST = "digest"

# The one timestamp shape the date-prefix match and the lexicographic ordering
# in this module are correct for.
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Fixed precision for the armed-level identity. Four decimals is finer than any
# quoted US equity price and coarse enough that a recomputed float still matches.
LEVEL_PRECISION = 4


def connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    # WAL would leave -wal/-shm sidecars beside a file that gets committed after
    # every run. DELETE keeps the database a single file; a cancelled write
    # leaves a rollback journal that SQLite replays on the next open.
    conn.execute("PRAGMA journal_mode = DELETE")
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


def _check_timestamp(name: str, value: str) -> str:
    if not TIMESTAMP_RE.match(value or ""):
        raise ValueError(
            f"{name} must be RFC3339 UTC of the form 2026-08-05T18:00:00Z, got {value!r}"
        )
    return value


def _level_key(level: float) -> str:
    return f"{float(level):.{LEVEL_PRECISION}f}"


def init_db(db: str | Path) -> None:
    """Apply `data/schema.sql`. Safe to run against an existing database —
    every statement in the schema is `IF NOT EXISTS`, so existing rows survive."""
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with _session(db) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_bars(db: str | Path, ticker: str, bars: Iterable[Bar], timeframe: str = DAILY) -> int:
    """Insert or replace bars for one ticker at one timeframe.

    `timeframe` is explicit and part of the key: writing intraday bars into the
    daily series is the silent corruption this whole module exists to prevent.

    `ON CONFLICT DO UPDATE` rather than `INSERT OR IGNORE`: a bar can legitimately
    be revised after the close (Alpaca corrects volume), and ignoring the second
    write would keep the stale figure forever.
    """
    rows = [(ticker, timeframe, b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]
    if not rows:
        return 0
    with _session(db) as conn:
        conn.executemany(
            """
            INSERT INTO bars (ticker, timeframe, ts, o, h, l, c, v)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, timeframe, ts) DO UPDATE SET
                o = excluded.o,
                h = excluded.h,
                l = excluded.l,
                c = excluded.c,
                v = excluded.v
            """,
            rows,
        )
    return len(rows)


def bars_for(
    db: str | Path, ticker: str, limit: int | None = None, timeframe: str = DAILY
) -> list[Bar]:
    """Stored bars for one ticker at one timeframe, oldest-first — the ordering
    every engine function assumes. `limit` takes the newest N and still returns
    them oldest-first.
    """
    with _session(db) as conn:
        if limit is None:
            rows = conn.execute(
                "SELECT ts, o, h, l, c, v FROM bars WHERE ticker = ? AND timeframe = ? "
                "ORDER BY ts ASC",
                (ticker, timeframe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, o, h, l, c, v FROM bars WHERE ticker = ? AND timeframe = ? "
                "ORDER BY ts DESC LIMIT ?",
                (ticker, timeframe, limit),
            ).fetchall()[::-1]
    return [Bar(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=int(r[5])) for r in rows]


def tickers_with_bars(db: str | Path, timeframe: str = DAILY) -> list[str]:
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM bars WHERE timeframe = ? ORDER BY ticker",
            (timeframe,),
        ).fetchall()
    return [r[0] for r in rows]


def bar_count(db: str | Path, ticker: str, timeframe: str = DAILY) -> int:
    with _session(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bars WHERE ticker = ? AND timeframe = ?",
            (ticker, timeframe),
        ).fetchone()[0]


def upsert_many(
    db: str | Path, bars_by_ticker: dict[str, Sequence[Bar]], timeframe: str = DAILY
) -> int:
    """Upsert every ticker's bars, returning the total rows written."""
    return sum(
        upsert_bars(db, ticker, bars, timeframe=timeframe)
        for ticker, bars in bars_by_ticker.items()
    )


def prune_bars(db: str | Path, keep: int, timeframe: str = DAILY) -> int:
    """Keep only the newest `keep` bars per ticker, returning rows deleted.

    `data.daily_bars_history` says bars are *kept*, not merely fetched. Without
    this the committed database grows without bound (RISKS.md R-7).
    """
    if keep <= 0:
        raise ValueError(f"keep must be positive, got {keep}")
    with _session(db) as conn:
        cursor = conn.execute(
            """
            DELETE FROM bars
            WHERE timeframe = ?
              AND rowid NOT IN (
                  SELECT rowid FROM bars b2
                  WHERE b2.ticker = bars.ticker AND b2.timeframe = bars.timeframe
                  ORDER BY b2.ts DESC LIMIT ?
              )
            """,
            (timeframe, keep),
        )
        return cursor.rowcount


def arm_level(
    db: str | Path,
    ticker: str,
    level: float,
    armed_on: date | None = None,
    direction: str = "both",
    source: str = "manual",
) -> None:
    """Arm a price level. Re-arming an already-armed or previously disarmed
    level updates it in place and makes it active — the user re-adding a level
    to `config/watchlist_core.yml` must mean "watch this", not "insert a
    duplicate" or "nothing happened".

    Note the lifecycle consequence: a level disarmed at post-close is re-armed
    by the next pre-market run for as long as it remains in the YAML. Whichever
    job owns removal owns it there, not here.
    """
    if direction not in ("above", "below", "both"):
        raise ValueError(f"direction must be above, below or both; got {direction!r}")
    day = (armed_on or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        conn.execute(
            """
            INSERT INTO armed_levels (ticker, level_key, level, direction, source, armed_on, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (ticker, level_key) DO UPDATE SET
                level = excluded.level,
                direction = excluded.direction,
                source = excluded.source,
                armed_on = excluded.armed_on,
                active = 1
            """,
            (ticker, _level_key(level), float(level), direction, source, day),
        )


def disarm_level(db: str | Path, ticker: str, level: float) -> int:
    """Deactivate a level after it triggers, returning the number of rows changed.

    The count is returned rather than swallowed: a disarm that matches nothing
    leaves the level firing on every subsequent poll, and a `None` return makes
    that indistinguishable from success.
    """
    with _session(db) as conn:
        cursor = conn.execute(
            "UPDATE armed_levels SET active = 0 WHERE ticker = ? AND level_key = ?",
            (ticker, _level_key(level)),
        )
        return cursor.rowcount


def armed_for(db: str | Path, ticker: str) -> list[float]:
    """Active armed levels for one ticker, ascending."""
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT level FROM armed_levels WHERE ticker = ? AND active = 1 ORDER BY level ASC",
            (ticker,),
        ).fetchall()
    return [float(r[0]) for r in rows]


def armed_details(db: str | Path, ticker: str) -> list[dict]:
    """Active armed levels with their direction and provenance, ascending.

    SPEC §6.3 ranks manually-armed levels first; `rule == 'armed_level'` cannot
    tell a hand-armed level from one a job armed, and `source` can.
    """
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT level, direction, source, armed_on FROM armed_levels "
            "WHERE ticker = ? AND active = 1 ORDER BY level ASC",
            (ticker,),
        ).fetchall()
    return [
        {"level": float(r[0]), "direction": r[1], "source": r[2], "armed_on": r[3]} for r in rows
    ]


def record_sent(
    db: str | Path,
    ticker: str,
    rule: str,
    level: float | None,
    bar_ts: str,
    sent_at: str | None = None,
    kind: str = KIND_INTRADAY,
) -> None:
    """Record that a message was delivered.

    Unique on `(ticker, rule, bar_ts)`: a retry after a partial failure must not
    consume a second slot of the intraday ceiling for a message the user only
    ever saw once.

    `kind` separates the two budgets in SPEC §6.3. A digest recorded as an
    intraday alert would eat the ceiling before the session opened.
    """
    if kind not in (KIND_INTRADAY, KIND_DIGEST):
        raise ValueError(f"kind must be {KIND_INTRADAY} or {KIND_DIGEST}; got {kind!r}")
    stamp = sent_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _check_timestamp("sent_at", stamp)
    _check_timestamp("bar_ts", bar_ts)
    with _session(db) as conn:
        conn.execute(
            """
            INSERT INTO sent_alerts (ticker, rule, kind, level, bar_ts, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, rule, bar_ts) DO NOTHING
            """,
            (ticker, rule, kind, None if level is None else float(level), bar_ts, stamp),
        )


def sent_count_today(db: str | Path, day: date | None = None, kind: str = KIND_INTRADAY) -> int:
    """How many messages of `kind` were delivered on `day` (UTC).

    `day` is explicit rather than read from the clock so a job can pass the
    trading day it is actually running for, and so this is testable without
    freezing time.

    The UTC day is safe for the current schedule and only for it: the ET session
    (09:30–16:00) and the 17:15 ET post-close run all fall inside one UTC
    calendar day under both EST and EDT. A pre-open or after-hours run would
    break that and this function with it.
    """
    target = (day or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM sent_alerts WHERE kind = ? AND substr(sent_at, 1, 10) = ?",
            (kind, target),
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


def armed_levels_sent_on(db: str | Path, day: date | None = None) -> list[tuple[str, float]]:
    """`(ticker, level)` for every armed level that alerted on `day` (UTC).

    The post-close job disarms what fired during the session, and the intraday
    job that sent those alerts is a separate cron process whose in-process list
    is long gone by 17:15. This table is the only record that crosses that
    boundary.
    """
    target = (day or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker, level FROM sent_alerts "
            "WHERE rule = 'armed_level' AND level IS NOT NULL "
            "AND substr(sent_at, 1, 10) = ?",
            (target,),
        ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def record_dropped(
    db: str | Path,
    ticker: str,
    rule: str,
    reason: str,
    bar_ts: str,
    level: float | None = None,
    dropped_at: str | None = None,
) -> None:
    """Record a trigger that qualified but lost to the ceiling.

    The intraday job and the post-close job are separate cron processes, so the
    in-process `dropped` list from `engine.ranking.rank` cannot otherwise reach
    the evening digest SPEC §6.3 requires it to appear in.
    """
    stamp = dropped_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _check_timestamp("dropped_at", stamp)
    _check_timestamp("bar_ts", bar_ts)
    with _session(db) as conn:
        conn.execute(
            """
            INSERT INTO dropped_alerts (ticker, rule, reason, level, bar_ts, dropped_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, rule, bar_ts) DO NOTHING
            """,
            (ticker, rule, reason, None if level is None else float(level), bar_ts, stamp),
        )


def dropped_on(db: str | Path, day: date | None = None) -> list[dict]:
    """Everything dropped on `day` (UTC), for the post-close digest."""
    target = (day or datetime.now(UTC).date()).isoformat()
    with _session(db) as conn:
        rows = conn.execute(
            "SELECT ticker, rule, reason, level, bar_ts, dropped_at FROM dropped_alerts "
            "WHERE substr(dropped_at, 1, 10) = ? ORDER BY dropped_at ASC",
            (target,),
        ).fetchall()
    return [
        {
            "ticker": r[0],
            "rule": r[1],
            "reason": r[2],
            "level": r[3],
            "bar_ts": r[4],
            "dropped_at": r[5],
        }
        for r in rows
    ]
