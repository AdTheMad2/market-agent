-- Market agent state. One SQLite file, committed to the repo (docs/SPEC.md §7).
--
-- Nothing personal is stored here: bars are public prices, armed levels are
-- public price levels, the alert tables record what was sent. No position
-- sizes, no cost basis, no account data — the repository is public.
--
-- Every write path must be idempotent. GitHub cron can double-fire, and a
-- re-run that duplicates or corrupts history is silent.

-- `timeframe` is part of the key, not metadata. Daily and 1-minute bars share
-- a (ticker, ts) keyspace without colliding, so intraday bars written into a
-- daily series would simply interleave — and every SMA, RSI and volume ratio
-- computed over the mixture would be wrong while looking entirely plausible.
CREATE TABLE IF NOT EXISTS bars (
    ticker    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,   -- 1Day | 1Min, as sent to the provider
    ts        TEXT    NOT NULL,   -- RFC3339 UTC, exactly as Alpaca returned it
    o         REAL    NOT NULL,
    h         REAL    NOT NULL,
    l         REAL    NOT NULL,
    c         REAL    NOT NULL,
    v         INTEGER NOT NULL,
    PRIMARY KEY (ticker, timeframe, ts)
);

-- Price levels to watch. `level_key` is the identity, not `level`: a REAL
-- primary key means a level recomputed with a last-bit difference silently
-- fails to match, so a disarm updates nothing and the level re-alerts forever.
-- `direction` mirrors config/watchlist_core.yml; `source` distinguishes a level
-- the user armed by hand from one a job armed, which SPEC §6.3's "manually
-- armed levels first" ranking needs and `rule == 'armed_level'` cannot express.
CREATE TABLE IF NOT EXISTS armed_levels (
    ticker    TEXT    NOT NULL,
    level_key TEXT    NOT NULL,   -- level rendered to fixed precision
    level     REAL    NOT NULL,
    direction TEXT    NOT NULL DEFAULT 'both',   -- above | below | both
    source    TEXT    NOT NULL DEFAULT 'manual', -- manual | job
    armed_on  TEXT    NOT NULL,   -- ISO date the level was armed
    active    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticker, level_key)
);

-- One row per message actually delivered. `kind` separates the two budgets in
-- SPEC §6.3 — two digests per day and at most three intraday alerts — which a
-- single undifferentiated count cannot: digests recorded here would otherwise
-- consume the intraday ceiling before the session opened.
CREATE TABLE IF NOT EXISTS sent_alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker  TEXT NOT NULL,
    rule    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'intraday',  -- intraday | digest
    level   REAL,
    bar_ts  TEXT NOT NULL,      -- the bar the alert fired on, never the run time
    sent_at TEXT NOT NULL,      -- RFC3339 UTC
    UNIQUE (ticker, rule, bar_ts)
);

-- Triggers that qualified but lost to the ceiling. The intraday job and the
-- post-close job are separate cron processes, so an in-process `dropped` list
-- cannot reach the evening digest that SPEC §6.3 requires it to appear in.
-- RISKS.md R-5 makes this count the primary instrument for OQ-4.
CREATE TABLE IF NOT EXISTS dropped_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    rule       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    level      REAL,
    bar_ts     TEXT NOT NULL,
    dropped_at TEXT NOT NULL,   -- RFC3339 UTC
    UNIQUE (ticker, rule, bar_ts)
);

-- No index on bars(ticker, timeframe, ts): the primary key already creates
-- sqlite_autoindex_bars_1 over exactly those columns, and a duplicate would
-- double index storage in a file committed up to 26 times a trading day
-- (RISKS.md R-7).
CREATE INDEX IF NOT EXISTS idx_sent_alerts_sent_at ON sent_alerts (sent_at);
CREATE INDEX IF NOT EXISTS idx_dropped_alerts_at ON dropped_alerts (dropped_at);
