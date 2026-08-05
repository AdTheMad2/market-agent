"""CLI entry point: a bars JSON fixture -> triggers -> suppressed -> ranked table.

`engine/` is pure by design (docs/SPEC.md Section 5.1); this module is the one
exception on purpose. It is the edge: the only place in `engine/` allowed to
touch the filesystem (loading `config/rules.yml` and the bars file) or read
argv. It contains no rule logic of its own -- it only loads inputs, calls
`triggers.evaluate` -> `suppressors.apply` -> `ranking.rank`, and formats the
result.

`today` for suppression is derived from the last bar's timestamp, not
`date.today()`, so a run against a committed fixture is reproducible rather
than depending on the day it happens to be executed (docs/SPEC.md Section
4.3: output must state the timestamp of the bar it was computed from, never
the run time).

See docs/SPEC.md Section 6.3 and docs/IMPLEMENTATION_PLAN.md Task 1.5.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from engine.ranking import rank
from engine.suppressors import SuppressionContext, Suppressed, SuppressorRules, apply
from engine.triggers import Bar, Rules, evaluate

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yml"


def _parse_armed(raw: str | None) -> list[float]:
    """`--armed` is a comma-separated list of price levels; absent means none armed."""
    if not raw:
        return []
    return [float(part) for part in raw.split(",") if part.strip()]


def _load_bars(path: Path) -> tuple[str, list[Bar]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ticker = data["symbol"]
    bars = [Bar.from_api(b) for b in data["bars"]]
    return ticker, bars


def _bar_date(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()


def _format_row(item: Suppressed) -> str:
    trigger = item.trigger
    volume_ratio = "n/a" if trigger.volume_ratio is None else f"{trigger.volume_ratio:.2f}x"
    rsi = "n/a" if trigger.rsi is None else f"{trigger.rsi:.1f}"
    reason = item.reason if item.reason is not None else "none"
    return (
        f"  {trigger.ticker:<6} {trigger.rule:<12} "
        f"level={trigger.level:>9.2f} price={trigger.price:>9.2f} "
        f"dist={trigger.distance_pct:>5.2f}% vol_ratio={volume_ratio:>7} "
        f"rsi={rsi:>5} bar={trigger.bar_timestamp} "
        f"demoted={'yes' if item.demoted else 'no'} reason={reason}"
    )


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which raises UnicodeEncodeError on
    # non-ASCII output. Output here is kept pure ASCII, but reconfigure
    # defensively in case that changes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Run evaluate -> apply -> rank against a bars JSON fixture and print the result."
    )
    parser.add_argument("bars_file", type=Path, help="Path to a bars JSON fixture (Alpaca bars shape).")
    parser.add_argument(
        "--armed",
        default=None,
        help="Comma-separated armed price levels, e.g. --armed 195.0,210.5 (default: none).",
    )
    args = parser.parse_args(argv)

    config = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    trigger_rules = Rules.from_mapping(config)
    suppressor_rules = SuppressorRules.from_mapping(config)
    ceiling = config["alerts"]["intraday_ceiling"]

    ticker, bars = _load_bars(args.bars_file)
    armed = _parse_armed(args.armed)

    triggers = evaluate(ticker, bars, trigger_rules, armed)

    today = _bar_date(bars[-1].t)
    context = SuppressionContext(
        rules=suppressor_rules,
        earnings_dates={},
        ex_dividend_dates={},
        macro_events=[],
        today=today,
    )
    suppressed = apply(triggers, context)

    to_send, dropped = rank(suppressed, already_sent_today=0, ceiling=ceiling)

    print(f"bars file: {args.bars_file} ({len(bars)} bars, last bar {bars[-1].t})")
    print(f"today (derived from last bar timestamp): {today.isoformat()}")
    print(
        "calendar context: earnings_dates={}, ex_dividend_dates={}, macro_events=[] "
        "(empty is correct for a fixture run, not a bug)"
    )
    print(f"intraday_ceiling: {ceiling}")
    print()

    if not triggers:
        print("no triggers fired")
        return 0

    print(f"to_send ({len(to_send)} of {len(triggers)} triggered):")
    if not to_send:
        print("  (none)")
    for item in to_send:
        print(_format_row(item))

    print()
    print(f"dropped by ceiling ({len(dropped)}):")
    if not dropped:
        print("  (none)")
    for item in dropped:
        print(_format_row(item))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
