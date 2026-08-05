"""Indicators over plain lists of floats.

No pandas, no numpy, no I/O. `engine/` is pure by design: the maths is insulated
from the scientific stack's release cycle (see docs/RISKS.md R-9, the host running
Python 3.14), and a failure here is a wrong number rather than a stack trace three
layers deep.

Every function takes values oldest-first and returns `None` rather than raising
when there is not enough data. `None` is a real answer during warm-up — a symbol
with 40 sessions of history genuinely has no 200-day average — and a scheduled run
must not die because one ticker is newly listed.

See docs/SPEC.md §5.2 and docs/IMPLEMENTATION_PLAN.md Task 1.1.
"""

from __future__ import annotations

from collections.abc import Sequence


def sma(closes: Sequence[float], period: int) -> float | None:
    """Simple moving average of the most recent `period` closes.

    None when fewer than `period` values are available.
    """
    if period <= 0 or len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Relative Strength Index using Wilder's smoothing.

    Wilder's, not a simple average of gains and losses: the two diverge on real
    data, and every published RSI level — the 70/30 in config/rules.yml included —
    refers to Wilder's. A simple-average implementation would place those
    thresholds at prices they were never meant to describe.

    Needs `period` changes, so `period + 1` closes. None below that.
    """
    if period <= 0 or len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Seed: plain averages over the first `period` changes.
    gains = [c if c > 0 else 0.0 for c in changes[:period]]
    losses = [-c if c < 0 else 0.0 for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder smoothing over everything after the seed window.
    for change in changes[period:]:
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        # No downside in the window. Flat prices produce no upside either, and
        # calling that "maximum strength" would fire an overbought alert on a
        # symbol that has not moved.
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_ratio(volumes: Sequence[int], period: int = 20) -> float | None:
    """Latest volume divided by the mean of the `period` sessions before it.

    The latest session is excluded from its own baseline. Including it damps the
    very spike the ratio exists to detect — on a 20-session window a genuine 2.5x
    day reads as 2.33x, which is the difference between clearing the 1.5
    threshold in config/rules.yml convincingly and clearing it by accident.

    Needs `period` baseline sessions plus the latest. None below that, and None
    when the baseline is zero — a halted or newly listed symbol must not raise
    ZeroDivisionError in the middle of a scheduled run.
    """
    if period <= 0 or len(volumes) < period + 1:
        return None

    baseline = volumes[-(period + 1) : -1]
    mean = sum(baseline) / period
    if mean == 0:
        return None

    return volumes[-1] / mean
