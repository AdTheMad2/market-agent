"""Run the whole chain against live providers without changing anything.

fetch → engine → rank → render → Telegram **test chat**. No live alert, no row
written, no level armed or disarmed. Safe to run at any hour, on any day,
including while the scheduled jobs are running.

**Why this exists separately from the jobs' own `--dry-run`.** A dry run of
`jobs.postclose` still writes bars, still arms levels, and refuses to do
anything at all outside a trading session. This script answers a different
question — *is every link in the chain intact right now* — and has to be
answerable on a Sunday, five minutes before a release, or straight after
changing any of the four subsystems it crosses. That last case is the whole
point: per the workspace rule, a script worth running once against a live
system is worth re-running after every change to it, so this one is committed
and re-run rather than retyped.

Isolation is structural, not careful: the database is a throwaway file in a
temp directory and `delivery.telegram` is pointed at `TELEGRAM_TEST_CHAT_ID`.
There is no path from here to `data/market.db` or to the live chat, so nothing
about the run depends on the reader having read this docstring.

Run:  python scripts/verify_pipeline.py [--no-send]
Exit: 0 every stage reported OK
      1 any stage failed — the stage that failed is named

See docs/SPEC.md §12 and docs/IMPLEMENTATION_PLAN.md Task 4.2.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sources  # noqa: E402
from delivery import telegram  # noqa: E402
from engine import ranking, suppressors  # noqa: E402
from engine.intraday import touched_levels  # noqa: E402
from engine.suppressors import SuppressionContext, SuppressorRules  # noqa: E402
from engine.triggers import Rules, evaluate  # noqa: E402
from jobs import market_calendar, watchlist  # noqa: E402
from render import template  # noqa: E402
from scripts._dotenv import load_env  # noqa: E402
from sources import alpaca, finnhub, fred, store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Enough history for the longest moving average in config/rules.yml, and no
# more: this script is a liveness check, not a backfill, and a full 250-bar
# fetch per name is quota spent to learn nothing extra.
PROBE_BARS = 210
PROBE_TICKERS = 2


class StageFailed(Exception):
    """A stage failed. Carries the stage name so the report can be specific."""


def _stage(name: str, fn, describe=None):
    """Run one stage and print exactly one line for it, pass or fail.

    `describe` turns the stage's return value into that line. Without it a
    stage returning a rendered message or a list of triggers would print the
    whole thing, and a report you have to scroll is a report nobody reads.
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 — this script's whole job is reporting
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        raise StageFailed(name) from None

    detail = describe(value) if describe else (value if isinstance(value, str) else "")
    print(f"  OK    {name}{f': {detail}' if detail else ''}")
    return value


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="run every stage but print the message instead of sending it",
    )
    args = parser.parse_args(argv)

    load_env(REPO_ROOT / ".env")

    if not args.no_send and not os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip():
        # Falling back to the live chat is what scripts/verify_delivery.py does,
        # and it is wrong here: that script sends one clearly-labelled test
        # message, while this one sends a rendered alert that is indistinguishable
        # from a real one. Refusing beats teaching the user to ignore alerts.
        print("FAIL - TELEGRAM_TEST_CHAT_ID is not set.")
        print("  This script renders a real-looking alert. Sending it to the live")
        print("  chat would teach you to doubt the ones that matter. Set a test")
        print("  chat, or re-run with --no-send.")
        return 1

    now = datetime.now(UTC)
    tickers = watchlist.core_tickers()[:PROBE_TICKERS]
    if not tickers:
        print("FAIL - config/watchlist_core.yml lists no tickers")
        return 1

    print(f"Verifying the pipeline against live providers at {now.isoformat()}")
    print(f"  probe: {', '.join(tickers)}\n")

    document = sources.rules_config()

    with tempfile.TemporaryDirectory(prefix="verify-pipeline-") as workspace:
        db = Path(workspace) / "throwaway.db"
        try:
            _stage("calendar", lambda: _calendar_line(now))
            _stage("store", lambda: _init(db))
            _stage("alpaca daily bars", lambda: _daily(db, tickers))
            _stage("alpaca intraday bars", lambda: _intraday(tickers))
            _stage("finnhub news", lambda: _news(tickers, now))
            _stage("finnhub earnings", lambda: _earnings(tickers))
            _stage("fred macro", _macro)
            triggers = _stage(
                "engine",
                lambda: _engine(document, db, tickers),
                describe=lambda t: f"{len(t)} trigger(s) on {len(tickers)} name(s)",
            )
            text = _stage(
                "render",
                lambda: _render(document, triggers, now),
                describe=lambda s: f"{len(s)} characters, {len(s.splitlines())} line(s)",
            )
            _stage("telegram", lambda: _deliver(text, args.no_send))
        except StageFailed as failed:
            print(f"\nFAILED at stage: {failed}")
            print("  Nothing was written and no live alert was sent.")
            return 1

    print("\nAll stages OK. No state was written and no live alert was sent.")
    return 0


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _calendar_line(now: datetime) -> str:
    session = "open" if market_calendar.is_market_open(now) else "shut"
    trading = "trading day" if market_calendar.is_trading_day(now.date()) else "not a session"
    # Reported, never enforced. The point of this script is that it answers on a
    # Sunday too — the jobs are the things that must respect the calendar.
    return f"{trading}, market currently {session}"


def _init(db: Path) -> str:
    store.init_db(db)
    return str(db.name)


def _daily(db: Path, tickers: list[str]) -> dict:
    bars = alpaca.daily_bars(tickers, days=PROBE_BARS)
    store.upsert_many(db, bars, timeframe=store.DAILY)
    counts = ", ".join(f"{t}={len(bars.get(t) or [])}" for t in tickers)
    if not any(bars.get(t) for t in tickers):
        raise RuntimeError("no daily bars returned for any probe ticker")
    return counts


def _intraday(tickers: list[str]) -> str:
    # Fetched inside the guard, exactly as jobs/intraday.py does, so this
    # exercises the guard rather than routing around it.
    with sources.intraday_run():
        bars = alpaca.intraday_bars(tickers)
    counts = ", ".join(f"{t}={len(bars.get(t) or [])}" for t in tickers)
    # An empty intraday window is normal outside the session, so it is reported
    # rather than failed — the request having succeeded is the thing under test.
    return counts or "no window"


def _news(tickers: list[str], now: datetime) -> str:
    by_ticker = finnhub.company_news(tickers, since=now.date())
    return f"{sum(len(v) for v in by_ticker.values())} headline(s) today"


def _earnings(tickers: list[str]) -> str:
    return f"{len(finnhub.earnings_calendar(14, tickers=tickers))} in 14 days"


def _macro() -> str:
    return f"{len(fred.macro_calendar(14))} watched release(s) in 14 days"


def _engine(document: dict, db: Path, tickers: list[str]) -> list:
    """Both engine paths: the daily rules and the intraday level comparison.

    The intraday path is exercised against a level derived from the last close
    rather than anything armed, so the check works on a database with no armed
    levels — which is the normal state of a fresh clone.
    """
    rules = Rules.from_mapping(document)
    triggers = []
    for ticker in tickers:
        bars = store.bars_for(db, ticker, timeframe=store.DAILY)
        if not bars:
            continue
        triggers.extend(evaluate(ticker, bars, rules, armed=[], watchlist="core"))

        # A level one cent under the last close is guaranteed to be touched, so
        # a silent break in `touched_levels` cannot hide behind a quiet market.
        probe_level = round(bars[-1].c - 0.01, 2)
        if probe_level > 0 and not touched_levels(
            ticker, bars[-1:], [{"level": probe_level, "direction": "both"}]
        ):
            raise RuntimeError(
                f"touched_levels missed a level inside {ticker}'s last bar range"
            )

    return triggers


def _render(document: dict, triggers: list, now: datetime) -> str:
    context = SuppressionContext(
        rules=SuppressorRules.from_mapping(document),
        earnings_dates={},
        ex_dividend_dates={},
        macro_events=[],
        today=now.date(),
    )
    suppressed = suppressors.apply(triggers, context)
    ordered, _ = ranking.rank(suppressed, already_sent_today=0, ceiling=len(suppressed))

    text = template.render_digest(
        ordered[: int(document["dashboard"]["max_setups"])],
        dropped=[],
        day=now.date(),
        title="Pipeline verification (not an alert)",
    )
    if not text.strip():
        raise RuntimeError("renderer produced an empty message")
    return text


def _deliver(text: str, no_send: bool) -> str:
    if not telegram.send(text, dry_run=no_send, to_test_chat=True):
        raise RuntimeError("telegram rejected the message; see the line above")
    return "printed" if no_send else "delivered to the test chat"


if __name__ == "__main__":
    sys.exit(main())
