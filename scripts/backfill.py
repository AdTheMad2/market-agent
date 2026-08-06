"""Fill data/market.db with daily history for every core-watchlist name.

Run:  python scripts/backfill.py [--db data/market.db] [--days 250]
Exit: 0 when every ticker ended up with the requested history
      1 when any ticker is short (a hole in the history is not a warning —
        every moving average computed over it would be quietly wrong)

**Safe to re-run.** `sources.store.upsert_bars` is idempotent, so a second run
adds zero rows; the script prints the row delta so that is visible rather than
assumed. That property is the Phase 2 observable check.

The bars come from the live Alpaca API, so this is one of the two scripts that
spends quota (the other is `scripts/verify_quotas.py`). Nothing in `tests/`
calls it.

See docs/IMPLEMENTATION_PLAN.md Task 2.4.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.watchlist import WATCHLIST_PATH, core_tickers  # noqa: E402,F401
from scripts._dotenv import load_env  # noqa: E402
from sources import alpaca, store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "config" / "rules.yml"

# `core_tickers` is re-exported from jobs.watchlist rather than defined twice.
# The jobs read the same file for the same reason, and a second parser here is
# the one that goes stale the day the file grows a key.


def history_length(path: Path | None = None) -> int:
    """`data.daily_bars_history` from config/rules.yml. No threshold in code."""
    data = yaml.safe_load((path or RULES_PATH).read_text(encoding="utf-8"))
    return int(data["data"]["daily_bars_history"])


def total_rows(db: Path) -> int:
    return sum(store.bar_count(db, ticker) for ticker in store.tickers_with_bars(db))


def main() -> int:
    # The Windows console defaults to cp1252 and mangles non-ASCII output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(store.DEFAULT_DB_PATH), help="SQLite file to fill")
    parser.add_argument("--days", type=int, default=None, help="bars per ticker (default: config)")
    args = parser.parse_args()

    load_env(REPO_ROOT / ".env")

    db = Path(args.db)
    days = args.days or history_length()
    # Passed explicitly rather than defaulted: the path is this module's to
    # choose, and the tests point it at a fixture.
    tickers = core_tickers(WATCHLIST_PATH)
    if not tickers:
        print("config/watchlist_core.yml lists no tickers; nothing to do")
        return 1

    store.init_db(db)
    before = total_rows(db)

    print(f"Backfilling {len(tickers)} tickers x {days} daily bars into {db}")
    fetched = alpaca.daily_bars(tickers, days=days)
    written = store.upsert_many(db, fetched, timeframe=store.DAILY)
    pruned = store.prune_bars(db, keep=days, timeframe=store.DAILY)

    after = total_rows(db)
    print(f"  rows written: {written}   pruned: {pruned}")
    print(f"  table rows: {before} -> {after} (delta {after - before})\n")

    short = []
    for ticker in tickers:
        count = store.bar_count(db, ticker)
        bars = store.bars_for(db, ticker, limit=1)
        latest = bars[0].t if bars else "n/a"
        marker = "OK  " if count >= days else "SHORT"
        print(f"  [{marker}] {ticker:<6} {count:>4} bars   latest {latest}")
        if count < days:
            short.append(f"{ticker} ({count}/{days})")

    if short:
        print(f"\n  SHORT: {', '.join(short)}")
        print("  A ticker short of its history is a hole every moving average will inherit.")
        return 1

    print(f"\n  Every ticker has {days} bars. Re-run this script within the same session:")
    print("  the delta must be 0. (Re-run on a later trading day and the delta is the")
    print("  new session's bars minus the pruned oldest ones - also 0, by design.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
