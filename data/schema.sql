-- Market agent state. One SQLite file, committed to the repo (docs/SPEC.md §7).
--
-- Nothing personal is stored here: bars are public prices, armed levels are
-- public price levels, sent_alerts records that a message was sent. No position
-- sizes, no cost basis, no account data — the repository is public.
--
-- Every write path must be idempotent. GitHub cron can double-fire, and a
-- re-run that duplicates or corrupts history is silent.

CREATE TABLE IF NOT EXISTS bars (
    ticker TEXT    NOT NULL,
    ts     TEXT    NOT NULL,   -- RFC3339 UTC, exactly as Alpaca returned it
    o      REAL    NOT NULL,
    h      REAL    NOT NULL,
    l      REAL    NOT NULL,
    c      REAL    NOT NULL,
    v      INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts)
);

-- Price levels the user armed by hand, plus levels written by the pre-market
-- job. `active` is 0 rather than deleted so a triggered level stays auditable.
CREATE TABLE IF NOT EXISTS armed_levels (
    ticker   TEXT    NOT NULL,
    level    REAL    NOT NULL,
    armed_on TEXT    NOT NULL,  -- ISO date the level was armed
    active   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticker, level)
);

-- One row per alert actually delivered. Feeds the intraday ceiling
-- (docs/SPEC.md §6.3) and the same-trigger cooldown.
CREATE TABLE IF NOT EXISTS sent_alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker  TEXT NOT NULL,
    rule    TEXT NOT NULL,
    level   REAL,
    bar_ts  TEXT NOT NULL,      -- the bar the alert fired on, never the run time
    sent_at TEXT NOT NULL,      -- RFC3339 UTC
    UNIQUE (ticker, rule, bar_ts)
);

CREATE INDEX IF NOT EXISTS idx_bars_ticker_ts ON bars (ticker, ts);
CREATE INDEX IF NOT EXISTS idx_sent_alerts_sent_at ON sent_alerts (sent_at);
