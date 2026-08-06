"""Intraday prices vs armed levels. The only comparison an intraday poll may make.

`engine/` is pure by design (docs/SPEC.md §5.1): no network, no filesystem, no
clock reads. `bar_timestamp` is the timestamp of the bar that touched the level,
never the time the poll ran.

**Why this module exists rather than reusing `engine.triggers.evaluate`.** An
intraday poll holds fifteen 1-minute bars. Fed to `evaluate` as if they were a
daily series, RSI(14) over those closes returns a plausible number that is pure
noise, and the 20-session range and every moving average are undefined. Any of
it firing would consume a slot of a ceiling of three
(docs/IMPLEMENTATION_PLAN.md Task 4.1, constraint b). So the intraday path
compares prices to levels and does nothing else.

**Why highs and lows rather than closes.** A level at 350.00 that trades to
350.40 and closes back at 349.80 was touched. A close-to-close test misses it,
and a missed touch is the one failure the user can never notice — nothing
arrives, and nothing is what a quiet market looks like too.

**The residual gap, stated rather than hidden.** A touch is decided from the
polled window alone. If price crossed a level entirely between the last bar of
one poll and the first bar of the next, and never revisited it, no touch is
seen. With 1-minute bars on liquid large caps that is rare, and closing it would
mean carrying the previous poll's last price as state for a case that mostly
resolves itself on the following poll.

See docs/SPEC.md §6.1 and docs/IMPLEMENTATION_PLAN.md Task 4.1.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from engine.triggers import Bar

DIRECTIONS = ("above", "below", "both")


@dataclass(frozen=True)
class Touch:
    """One armed level reached during one poll window."""

    ticker: str
    level: float
    price: float
    distance_pct: float
    direction: str
    bar_timestamp: str
    window_high: float
    window_low: float


def _reached(bar: Bar, level: float, direction: str) -> bool:
    """Did this bar reach `level` in the armed direction?

    `above` and `below` are one-sided on purpose. "Alert me if it goes past X"
    must still fire when the move was fast enough that the level never sat
    inside a single bar's range — a gap through a level is the move most worth
    hearing about, and a two-sided test would go silent on exactly that.
    `both` is the two-sided test: the level lay inside the traded range.
    """
    if direction == "above":
        return bar.h >= level
    if direction == "below":
        return bar.l <= level
    return bar.l <= level <= bar.h


def touched_levels(
    ticker: str,
    bars: Sequence[Bar],
    armed: Sequence[Mapping],
) -> list[Touch]:
    """Which of `armed` the price reached during `bars`, in armed order.

    `armed` is `sources.store.armed_details` output: `{level, direction, ...}`.
    Order is preserved rather than sorted, so the caller's ranking decides
    precedence and this function decides only what is true.

    A non-positive level is skipped rather than raising. It is caller data out
    of SQLite, and one bad row must not cost the other levels — the same
    handling `engine.triggers.evaluate` gives the same input.
    """
    if not bars or not armed:
        return []

    latest_close = bars[-1].c
    window_high = max(b.h for b in bars)
    window_low = min(b.l for b in bars)

    touches: list[Touch] = []
    for entry in armed:
        try:
            level = float(entry["level"])
        except (KeyError, TypeError, ValueError):
            continue
        if level <= 0:
            continue

        direction = str(entry.get("direction", "both")).lower()
        if direction not in DIRECTIONS:
            # An unrecognised direction watches both sides rather than raising:
            # the user asked to be told about this level, and the strictest
            # reading of a typo is the one that stays silent.
            direction = "both"

        first = next((b for b in bars if _reached(b, level, direction)), None)
        if first is None:
            continue

        touches.append(
            Touch(
                ticker=ticker,
                level=level,
                price=latest_close,
                distance_pct=abs(latest_close - level) / level * 100.0,
                direction=direction,
                bar_timestamp=first.t,
                window_high=window_high,
                window_low=window_low,
            )
        )

    return touches
