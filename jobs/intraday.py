"""The intraday poll: armed levels, the ceiling, and nothing else.

Runs every 15 minutes across the session. Four things about it are deliberate
and each of them is a bug if quietly reverted.

**The whole body runs inside `sources.intraday_run()`.** That context is the
only thing arming the news guard; every news, earnings, filing and macro fetch
calls `sources.forbid_intraday()` and a job that forgets the wrapper is not
protected. Finnhub bills one call per symbol, so a news sweep on every poll
would burn the daily quota before lunch (docs/SPEC.md §4.2). The wrapper is
asserted in `tests/jobs/test_intraday.py` — it is the one thing here that fails
silently.

**1-minute bars never reach `engine.triggers.evaluate`.** They go to
`engine.intraday.touched_levels`, which compares prices to levels and nothing
else. See that module for why RSI over fifteen one-minute closes is worse than
useless.

**Intraday bars are not persisted.** The database is committed on every run and
the repository is public; roughly 1,500 rows a day per hundred names, kept
forever, is a growth curve with no ceiling and no reader (docs/RISKS.md R-7).
Nothing downstream reads them: the digests recompute from daily bars. If that
ever changes, `store.upsert_bars` must be called with `timeframe=store.INTRADAY`
— daily and 1-minute bars do not collide on `(ticker, ts)`, so mixing them
corrupts every moving average silently.

**A level is disarmed the moment it alerts.** `store.disarm_level` returns a row
count and a zero is logged rather than assumed, because a disarm that matched
nothing leaves the level firing on every remaining poll of the session. The
cooldown is the backstop for exactly that case.

**Known gap: suppressors do not run here.** Earnings and macro dates come from
network calls this job is forbidden to make, so an armed level firing the day
before earnings carries no Caution line. Closing it means persisting the
calendar during the digest runs and reading it back here; that is a real
improvement and it is not Phase 4's scope. Stated because a missing Caution
looks identical to no caution being warranted.

Run:  python -m jobs.intraday [--dry-run] [--db data/market.db]
Exit: 0 always, unless the run itself failed — a poll with nothing to say is
      the normal case and must not colour the Actions run red

See docs/SPEC.md §6.3 and docs/IMPLEMENTATION_PLAN.md Task 4.1.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sources
from delivery import telegram
from engine import ranking, suppressors
from engine.intraday import Touch, touched_levels
from engine.suppressors import SuppressionContext, SuppressorRules
from engine.triggers import Trigger
from jobs import market_calendar, watchlist
from jobs.cli import parse_args, prepare
from render import template
from sources import alpaca, store


@dataclass
class IntradayOutcome:
    """What a poll did, so the caller can log it and tests can assert."""

    ran: bool
    touched: int = 0
    sent: int = 0
    dropped: int = 0
    errors: list[str] = field(default_factory=list)


def _as_trigger(touch: Touch) -> Trigger:
    """A `Touch` in the shape the ranking and the renderer already speak.

    `rule="armed_level"` is the exact string `engine.ranking` sorts to the front
    and `render/template.py` phrases — both were written against it in Phase 1.
    `volume_ratio` and `rsi` are `None` rather than computed: over fifteen
    one-minute bars they would be noise, and the renderer already prints `n/a`.
    """
    return Trigger(
        ticker=touch.ticker,
        rule="armed_level",
        level=touch.level,
        price=touch.price,
        distance_pct=touch.distance_pct,
        volume_ratio=None,
        rsi=None,
        bar_timestamp=touch.bar_timestamp,
        watchlist="core",
        detail=f"armed {touch.direction}",
    )


def _empty_context(document: dict, moment: datetime) -> SuppressionContext:
    """A suppression context with no calendar in it. See the module docstring."""
    return SuppressionContext(
        rules=SuppressorRules.from_mapping(document),
        earnings_dates={},
        ex_dividend_dates={},
        macro_events=[],
        today=moment.date(),
    )


def _in_cooldown(db: Path, touch: Touch, minutes: int, now: datetime) -> bool:
    """Has this exact level alerted inside the cooldown window?

    Per level, not per ticker: a name with three armed levels must not go quiet
    on all three because one of them fired. This is a backstop — the primary
    guard is disarming on send — and it is what saves the session when a disarm
    matches nothing.
    """
    stamp = store.last_sent(db, touch.ticker, "armed_level", level=touch.level)
    if stamp is None:
        return False
    try:
        sent_at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        # An unparseable stamp cannot be reasoned about. Treating it as "not in
        # cooldown" risks a duplicate alert; treating it as "in cooldown" risks
        # silence. Silence is the failure nobody notices, so the alert wins.
        return False
    return now - sent_at < timedelta(minutes=minutes)


def run(
    *,
    db: Path | None = None,
    moment: datetime | None = None,
    dry_run: bool = False,
    to_test_chat: bool = False,
    fetch_bars=None,
) -> IntradayOutcome:
    """One poll, start to finish, inside the intraday guard.

    `fetch_bars` defaults to `alpaca.intraday_bars` and exists so
    `scripts/verify_intraday.py` can replay a past session window through this
    exact code path. It is a seam, not a mode: nothing else about the run
    changes, so a rehearsal that passes proves the scheduled poll and not a
    parallel implementation of it.
    """
    with sources.intraday_run():
        return _run(
            db=db,
            moment=moment,
            dry_run=dry_run,
            to_test_chat=to_test_chat,
            fetch_bars=fetch_bars or alpaca.intraday_bars,
        )


def _run(
    *,
    db: Path | None,
    moment: datetime | None,
    dry_run: bool,
    to_test_chat: bool,
    fetch_bars,
) -> IntradayOutcome:
    db = Path(db or store.DEFAULT_DB_PATH)
    now = moment or datetime.now(UTC)

    if not market_calendar.is_market_open(now):
        print(f"{now.isoformat()} is outside a live session; nothing to do")
        return IntradayOutcome(ran=False)

    document = sources.rules_config()
    ceiling = int(document["alerts"]["intraday_ceiling"])
    cooldown = int(document["alerts"]["same_trigger_cooldown_minutes"])
    tickers = watchlist.core_tickers()
    errors: list[str] = []

    store.init_db(db)

    armed_by_ticker = {t: store.armed_details(db, t) for t in tickers}
    watching = [t for t, levels in armed_by_ticker.items() if levels]
    if not watching:
        # No fetch at all. Nothing is armed, so there is no question to ask, and
        # 26 polls a session asking it anyway is quota spent on a known answer.
        print("no armed levels; skipping the fetch entirely")
        return IntradayOutcome(ran=True)

    bars_by_ticker = fetch_bars(watching)

    touches: list[Touch] = []
    for ticker in watching:
        bars = bars_by_ticker.get(ticker) or []
        if not bars:
            errors.append(f"bars: {ticker} returned no intraday bars")
            continue
        touches.extend(touched_levels(ticker, bars, armed_by_ticker[ticker]))

    fresh = [t for t in touches if not _in_cooldown(db, t, cooldown, now)]
    for skipped in (t for t in touches if t not in fresh):
        print(f"cooldown: {skipped.ticker} {skipped.level} alerted within {cooldown}m; holding")

    suppressed = suppressors.apply([_as_trigger(t) for t in fresh], _empty_context(document, now))

    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    already = store.sent_count_today(db, now.date(), kind=store.KIND_INTRADAY)
    to_send, dropped = ranking.rank(suppressed, already_sent_today=already, ceiling=ceiling)

    sent = 0
    for item in to_send:
        trigger = item.trigger
        if not telegram.send(template.render_alert(item), dry_run=dry_run,
                             to_test_chat=to_test_chat):
            errors.append(f"delivery: {trigger.ticker} {trigger.level} not sent")
            continue
        sent += 1
        if dry_run:
            continue
        store.record_sent(
            db,
            trigger.ticker,
            trigger.rule,
            level=trigger.level,
            bar_ts=trigger.bar_timestamp,
            # The run's own moment, not wall-clock. `sent_count_today` and
            # `dropped_on` both match on the date prefix of these columns, so a
            # run for an explicit moment must file under that moment or its own
            # ceiling arithmetic stops seeing what it just sent.
            sent_at=stamp,
            kind=store.KIND_INTRADAY,
        )
        # Disarm only after the message is known to have landed. Disarming first
        # would lose the level to a transient 502 at Telegram.
        if store.disarm_level(db, trigger.ticker, trigger.level) == 0:
            errors.append(
                f"disarm: {trigger.ticker} {trigger.level} matched no row; "
                "the cooldown is now the only thing stopping a repeat"
            )

    if not dry_run:
        for item in dropped:
            trigger = item.trigger
            # The post-close job is a separate cron process and cannot see
            # `rank`'s in-process list. This table is how a dropped alert
            # reaches the evening digest (docs/SPEC.md §6.3).
            store.record_dropped(
                db,
                trigger.ticker,
                trigger.rule,
                reason=f"daily ceiling of {ceiling} reached",
                bar_ts=trigger.bar_timestamp,
                level=trigger.level,
                dropped_at=stamp,
            )

    print(f"{len(touches)} touch(es); {sent} sent, {len(dropped)} dropped, {already} already today")
    for message in errors:
        print(f"WARN - {message}")

    return IntradayOutcome(
        ran=True, touched=len(touches), sent=sent, dropped=len(dropped), errors=errors
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(__doc__, argv)
    prepare()
    outcome = run(db=args.db, dry_run=args.dry_run, to_test_chat=args.test_chat)
    # A quiet poll is the normal case. Only a delivery failure is worth a red
    # run — 26 red runs a day would train everyone to ignore the Actions tab.
    return 1 if any(e.startswith("delivery:") for e in outcome.errors) else 0


if __name__ == "__main__":
    sys.exit(main())
