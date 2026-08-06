"""The digest pipeline both scheduled runs share.

`jobs/premarket.py` and `jobs/postclose.py` differ in four things — when they
run, what they call themselves, whether they arm levels from the YAML, and
whether they disarm levels that fired. Everything between the calendar check and
the send is identical, so it lives here once. Two copies of a fetch-engine-render
chain drift, and the way they drift is that one of them keeps working.

**This module is never entered from an intraday run.** It fetches news, earnings,
filings and macro dates, all of which call `sources.forbid_intraday`. That guard
is armed by `sources.intraday_run()`, which the Phase 4 intraday job enters and
this one must not.

Order of operations is deliberate: state is written *before* the message is sent.
A delivery failure then costs a message, not the run's bars. The reverse order
loses data to a transient 502 at Telegram.

See docs/SPEC.md §6 and docs/IMPLEMENTATION_PLAN.md Tasks 3.3 and 3.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from delivery import telegram
from engine import ranking, suppressors
from engine.suppressors import SuppressionContext, SuppressorRules
from engine.triggers import Rules, evaluate
from jobs import market_calendar, watchlist
from render import template
from sources import Earnings, MacroEvent, NewsItem, alpaca, edgar, finnhub, fred, store

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "config" / "rules.yml"

PREMARKET = "premarket"
POSTCLOSE = "postclose"

TITLES = {PREMARKET: "Pre-market digest", POSTCLOSE: "Post-close digest"}

# How far forward the earnings and macro calendars are read. Wide enough to
# cover the longest suppressor window in config/rules.yml with room to show the
# user what is coming, and no wider — every extra day is calendar noise.
CALENDAR_HORIZON_DAYS = 14

# `_digest` is not a ticker. `sent_alerts` is unique on (ticker, rule, bar_ts),
# and this sentinel gives the two daily digests one row each without colliding
# with any real symbol.
DIGEST_TICKER = "_digest"


@dataclass
class DigestOutcome:
    """What a run did, so the caller can set an exit code and tests can assert."""

    ran: bool
    day: date
    kind: str
    triggers: int = 0
    delivered: bool = False
    text: str = ""
    errors: list[str] = field(default_factory=list)


def _rules_document() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def _context(
    document: dict,
    day: date,
    earnings: list[Earnings],
    macro: list[MacroEvent],
) -> SuppressionContext:
    """Calendar context for the suppressors.

    Ex-dividend dates are empty: no source in docs/SPEC.md §4.1 publishes them
    on a free tier. The window stays configured and the suppressor stays wired,
    so adding a source later is a one-line change rather than a redesign. Left
    visible here rather than hidden behind a default so the gap is not mistaken
    for a working feature (docs/RISKS.md).
    """
    earnings_dates: dict[str, date] = {}
    for row in earnings:
        try:
            parsed = date.fromisoformat(row.date)
        except (TypeError, ValueError):
            continue
        # Earliest scheduled date wins: a name reporting twice inside the window
        # (a restatement, a duplicated row) should suppress from the nearer one.
        if row.ticker not in earnings_dates or parsed < earnings_dates[row.ticker]:
            earnings_dates[row.ticker] = parsed

    macro_dates: list[date] = []
    for event in macro:
        try:
            macro_dates.append(date.fromisoformat(event.date))
        except (TypeError, ValueError):
            continue

    return SuppressionContext(
        rules=SuppressorRules.from_mapping(document),
        earnings_dates=earnings_dates,
        ex_dividend_dates={},
        macro_events=macro_dates,
        today=day,
    )


def _fetch_context(
    tickers: list[str], day: date, errors: list[str]
) -> tuple[list[NewsItem], list[Earnings], list[MacroEvent]]:
    """News, earnings and macro dates, each failing independently.

    A provider outage must cost its own section and nothing else. The digest is
    the day's only user-visible output; delivering it without headlines beats
    not delivering it. Every failure is collected and reported in the run log so
    a silent degradation is still a visible one.
    """
    news: list[NewsItem] = []
    earnings: list[Earnings] = []
    macro: list[MacroEvent] = []

    try:
        by_ticker = finnhub.company_news(tickers, since=day)
        news = [item for items in by_ticker.values() for item in items]
    except Exception as exc:  # noqa: BLE001 — one section, not the digest
        errors.append(f"news: {type(exc).__name__}: {exc}")

    try:
        earnings = finnhub.earnings_calendar(CALENDAR_HORIZON_DAYS, tickers=tickers)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"earnings: {type(exc).__name__}: {exc}")

    try:
        macro = fred.macro_calendar(CALENDAR_HORIZON_DAYS)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"macro: {type(exc).__name__}: {exc}")

    try:
        # Filings are fetched but not yet rendered: SPEC §10 gives them a
        # dashboard block (Phase 6), and the call is here so the quota and the
        # User-Agent are exercised twice a day rather than first discovered
        # broken in Phase 6.
        edgar.recent_filings(tickers)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"filings: {type(exc).__name__}: {exc}")

    return news, earnings, macro


def _arm_from_config(db: Path, day: date) -> int:
    """Arm every level listed in `config/watchlist_core.yml`.

    Re-arming is idempotent (`store.arm_level` updates in place), which is what
    makes the lifecycle work: the post-close run disarms a level that fired, and
    the next pre-market run re-arms it for as long as it is still in the YAML.
    Whichever job owns removal owns it in the YAML, not in the table.
    """
    levels = watchlist.armed_levels()
    for entry in levels:
        store.arm_level(
            db,
            entry["ticker"],
            entry["level"],
            armed_on=day,
            direction=entry["direction"],
            source="job",
        )
    return len(levels)


def _disarm_triggered(db: Path, day: date) -> int:
    """Disarm levels that fired today, reading `sent_alerts` rather than memory.

    The intraday job that sent them is a separate cron process, so its
    in-process list is long gone by 17:15. `sent_alerts` is the only record that
    crosses the process boundary.
    """
    disarmed = 0
    for ticker, level in store.armed_levels_sent_on(db, day):
        disarmed += store.disarm_level(db, ticker, level)
    return disarmed


def run(
    kind: str,
    *,
    db: Path | None = None,
    day: date | None = None,
    dry_run: bool = False,
    to_test_chat: bool = False,
) -> DigestOutcome:
    """Run one digest end to end. See the module docstring for the ordering rule."""
    if kind not in (PREMARKET, POSTCLOSE):
        raise ValueError(f"kind must be {PREMARKET} or {POSTCLOSE}; got {kind!r}")

    db = Path(db or store.DEFAULT_DB_PATH)
    day = day or datetime.now(UTC).date()

    if not market_calendar.is_trading_day(day):
        print(f"{day.isoformat()} is not an NYSE session; nothing to do")
        return DigestOutcome(ran=False, day=day, kind=kind)

    document = _rules_document()
    rules = Rules.from_mapping(document)
    history = int(document["data"]["daily_bars_history"])
    tickers = watchlist.core_tickers()
    errors: list[str] = []

    if not tickers:
        raise RuntimeError("config/watchlist_core.yml lists no tickers")

    store.init_db(db)

    # 1. Bars, persisted first. Everything downstream reads the store rather
    #    than the fetch result, so a partial fetch degrades to yesterday's close
    #    instead of an empty series.
    fetched = alpaca.daily_bars(tickers, days=history)
    store.upsert_many(db, fetched, timeframe=store.DAILY)
    store.prune_bars(db, keep=history, timeframe=store.DAILY)

    # 2. Arm before evaluating, so a level added to the YAML this morning is
    #    live in this very digest rather than from tomorrow.
    if kind == PREMARKET:
        armed_count = _arm_from_config(db, day)
        print(f"armed {armed_count} level(s) from config/watchlist_core.yml")

    news, earnings, macro = _fetch_context(tickers, day, errors)
    context = _context(document, day, earnings, macro)

    triggers = []
    for ticker in tickers:
        bars = store.bars_for(db, ticker, timeframe=store.DAILY)
        if not bars:
            errors.append(f"bars: {ticker} has no stored history")
            continue
        triggers.extend(
            evaluate(ticker, bars, rules, armed=store.armed_for(db, ticker), watchlist="core")
        )

    suppressed = suppressors.apply(triggers, context)

    # A digest is not rationed by the intraday ceiling (SPEC §6.3: 2 digests
    # *plus* 3 intraday alerts), so `rank` is used here purely for its ordering:
    # a ceiling of len() splits nothing off.
    ordered, _ = ranking.rank(suppressed, already_sent_today=0, ceiling=len(suppressed))
    shown = ordered[: int(document["dashboard"]["max_setups"])]

    text = template.render_digest(
        shown,
        dropped=store.dropped_on(db, day),
        day=day,
        title=TITLES[kind],
        news=news,
        earnings=earnings,
        macro=macro,
    )

    delivered = telegram.send(text, dry_run=dry_run, to_test_chat=to_test_chat)

    if delivered and not dry_run:
        # kind="digest" keeps the two daily digests out of the intraday budget.
        #
        # `sent_at` carries the run's date with the wall-clock time of day, not
        # `datetime.now()` wholesale. `store.sent_count_today` counts by the date
        # prefix of this column, so a `--day` re-run of a missed session would
        # otherwise file its digest under today and read as zero digests sent for
        # the day it actually ran for. The time-of-day is real either way, which
        # is what `store.last_sent` orders on. In the normal case day is today
        # and this is exactly now.
        now = datetime.now(UTC)
        store.record_sent(
            db,
            DIGEST_TICKER,
            kind,
            level=None,
            bar_ts=f"{day.isoformat()}T00:00:00Z",
            sent_at=f"{day.isoformat()}T{now.strftime('%H:%M:%S')}Z",
            kind=store.KIND_DIGEST,
        )

    if kind == POSTCLOSE:
        print(f"disarmed {_disarm_triggered(db, day)} level(s) that fired today")

    for message in errors:
        print(f"WARN - {message}")

    return DigestOutcome(
        ran=True,
        day=day,
        kind=kind,
        triggers=len(ordered),
        delivered=delivered,
        text=text,
        errors=errors,
    )
