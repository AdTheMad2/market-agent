"""Replay a real session window through the intraday poll, changing nothing.

The scheduled poll can only be watched while the market is open, which is six
and a half hours out of every twenty-four and none of the weekend. This script
takes real 1-minute bars from a window that has already happened and runs them
through `jobs.intraday.run` — the same function the workflow calls, through the
same seam, with the same ranking, ceiling and renderer.

**What it proves:** that live provider data, the levels currently in
`config/watchlist_core.yml`, and the ceiling arithmetic agree on who gets an
alert. That is the mechanism behind Task 4.1 Steps 3 and 4.

**What it cannot prove:** that the schedule fires unprompted. Nothing run by
hand can prove that, which is exactly why the plan's observable check is worded
the way it is.

Isolation is structural: a throwaway database in a temp directory, levels armed
into it rather than into `data/market.db`, and delivery defaulting to a dry run.
Pass `--send` to actually deliver — the alerts are indistinguishable from real
ones, so that is opt-in and never the default.

Run:  python scripts/verify_intraday.py [--at 2026-08-06T15:00Z] [--minutes 15] [--send]
Exit: 0 the replay produced the split the ranking predicts
      1 no bars, no armed levels, or a delivery failure

See docs/IMPLEMENTATION_PLAN.md Task 4.1 Steps 3-4.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs import intraday, market_calendar, watchlist  # noqa: E402
from scripts._dotenv import load_env  # noqa: E402
from sources import alpaca, store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Matches the intraday cron interval, so the replayed window is the same shape
# the scheduled poll would have seen.
DEFAULT_WINDOW_MINUTES = 15


def _default_moment() -> datetime:
    """Mid-session on the most recent trading day that has one.

    Today when today's session has already opened, otherwise the previous
    session — so the script is runnable at 23:00 on a Friday, which is exactly
    when someone wants to check their work.
    """
    now = datetime.now(UTC)
    day = now.date()
    bounds = market_calendar.session_bounds(day)
    if bounds is None or now < bounds[0] + timedelta(minutes=DEFAULT_WINDOW_MINUTES):
        day = market_calendar.previous_trading_day(day)
        bounds = market_calendar.session_bounds(day)
    opened, closed = bounds
    # An hour into the session: past the opening auction's thin, erratic prints
    # and comfortably inside even a half-day close.
    return min(opened + timedelta(hours=1), closed - timedelta(minutes=1))


def _parse_moment(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--at must carry a timezone, e.g. 2026-08-06T15:00Z")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", type=_parse_moment, default=None,
                        help="UTC instant the window ends at (default: mid-session, last session)")
    parser.add_argument("--minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
                        help="length of the replayed window")
    parser.add_argument("--send", action="store_true",
                        help="deliver the alerts for real; off by default")
    args = parser.parse_args(argv)

    load_env(REPO_ROOT / ".env")

    moment = args.at or _default_moment()
    if not market_calendar.is_market_open(moment):
        print(f"FAIL - {moment.isoformat()} is not inside a session; nothing to replay")
        return 1

    levels = watchlist.armed_levels()
    if not levels:
        print("FAIL - config/watchlist_core.yml declares no armed levels.")
        print("  There is nothing for an intraday poll to check. Add a level, or")
        print("  accept that the intraday job is a no-op by design until you do.")
        return 1

    window_start = moment - timedelta(minutes=args.minutes)
    tickers = sorted({entry["ticker"] for entry in levels})

    print(f"Replaying {window_start.strftime('%H:%M')}-{moment.strftime('%H:%M')} UTC "
          f"on {moment.date().isoformat()}")
    print(f"  armed: {len(levels)} level(s) across {', '.join(tickers)}")
    print(f"  delivery: {'LIVE' if args.send else 'dry run'}\n")

    # `_bars` rather than `intraday_bars`: the public function always ends its
    # window at the SIP delay from now, which is the correct behaviour for the
    # job and the wrong one for a replay of a window that has already passed.
    replayed = alpaca._bars(
        tickers, alpaca.INTRADAY_TIMEFRAME, window_start, moment, limit=None
    )
    counts = {t: len(replayed.get(t) or []) for t in tickers}
    print(f"  bars fetched: {counts}")
    if not any(counts.values()):
        print("\nFAIL - the provider returned no bars for that window.")
        return 1

    with tempfile.TemporaryDirectory(prefix="verify-intraday-") as workspace:
        db = Path(workspace) / "throwaway.db"
        store.init_db(db)
        for entry in levels:
            store.arm_level(db, entry["ticker"], entry["level"], armed_on=moment.date(),
                            direction=entry["direction"], source="job")

        print()
        outcome = intraday.run(
            db=db,
            moment=moment,
            dry_run=not args.send,
            fetch_bars=lambda watching: {t: replayed.get(t, []) for t in watching},
        )

        print(f"\n  touched: {outcome.touched}   sent: {outcome.sent}   "
              f"dropped: {outcome.dropped}")

        dropped_rows = store.dropped_on(db, moment.date())
        for row in dropped_rows:
            print(f"    held back: {row['ticker']} at {row['level']} — {row['reason']}")
        if not args.send:
            # Said out loud because the alternative reading — "nothing was
            # dropped" — is the opposite of what the counts above show.
            print("    (a dry run writes no rows; the counts above are what the")
            print("     real run would have recorded and disarmed)")

        still_armed = {t: store.armed_for(db, t) for t in tickers}
        print(f"  still armed after the run: {still_armed}")

    for message in outcome.errors:
        print(f"  WARN - {message}")

    if outcome.touched == 0:
        print("\nFAIL - no level was touched, so nothing downstream was exercised.")
        return 1
    if any(e.startswith("delivery:") for e in outcome.errors):
        print("\nFAILED at delivery.")
        return 1

    print("\nReplay OK. Nothing was written to data/market.db.")
    print("  This proves the mechanism, not the schedule — only an unprompted")
    print("  scheduled run can prove that (docs/IMPLEMENTATION_PLAN.md Task 4.1 Step 4).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
