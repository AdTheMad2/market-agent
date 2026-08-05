"""Bars + thresholds -> Trigger records.

`engine/` is pure by design (see docs/SPEC.md Section5.1): no network, no
filesystem, no clock reads. `evaluate` never calls `datetime.now` -- the time
of an alert is the timestamp of the bar it fired on, not the time the scan
happened to run, because scheduled runs are delayed and the message must not
lie about that (docs/SPEC.md Section4.3).

Every numeric threshold comes from `rules`, built from the `triggers:`
section of `config/rules.yml`. YAML loading happens in the caller; this
module only ever receives already-parsed values.

See docs/SPEC.md Section5.2 and docs/IMPLEMENTATION_PLAN.md Task 1.2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from engine.indicators import rsi, sma, volume_ratio


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar, oldest-first ordering assumed by every caller."""

    t: str
    o: float
    h: float
    l: float
    c: float
    v: int

    @classmethod
    def from_api(cls, data: Mapping) -> "Bar":
        """Build a Bar from an Alpaca bar dict (`sources/alpaca.py`'s job)."""
        return cls(t=data["t"], o=data["o"], h=data["h"], l=data["l"], c=data["c"], v=data["v"])


@dataclass(frozen=True)
class Rules:
    """The `triggers:` section of config/rules.yml, already parsed."""

    ma_periods: tuple[int, ...]
    ma_proximity_pct: float
    ma_cross_enabled: bool
    range_break_days: int
    volume_period: int
    volume_multiple: float
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    rsi_extreme_overbought: float
    rsi_extreme_oversold: float

    @classmethod
    def from_mapping(cls, data: Mapping) -> "Rules":
        """Build from the full parsed config/rules.yml (reads its `triggers:` key)."""
        triggers = data["triggers"]
        return cls(
            ma_periods=tuple(triggers["ma_periods"]),
            ma_proximity_pct=triggers["ma_proximity_pct"],
            ma_cross_enabled=triggers["ma_cross_enabled"],
            range_break_days=triggers["range_break_days"],
            volume_period=triggers["volume_period"],
            volume_multiple=triggers["volume_multiple"],
            rsi_period=triggers["rsi_period"],
            rsi_overbought=triggers["rsi_overbought"],
            rsi_oversold=triggers["rsi_oversold"],
            rsi_extreme_overbought=triggers["rsi_extreme_overbought"],
            rsi_extreme_oversold=triggers["rsi_extreme_oversold"],
        )


@dataclass(frozen=True)
class Trigger:
    """One rule firing on one bar. `rule` is one of the five exact strings
    below -- later phases (ranking, rendering) match on them.
    """

    ticker: str
    rule: str
    level: float
    price: float
    distance_pct: float
    volume_ratio: float | None
    rsi: float | None
    bar_timestamp: str
    watchlist: str


def _distance_pct(price: float, level: float) -> float | None:
    """`None` if `level` cannot produce a meaningful distance, rather than a
    `ZeroDivisionError` that would crash a scheduled run. `level` for most
    rules is computed here (an SMA, a prior high/low), but for `armed_level`
    it comes from caller data -- `--armed` CLI floats today, a DB value in
    a later phase -- so a non-positive level is reachable and must be
    skipped, not trusted (see engine/indicators.py's return-None philosophy).
    """
    if level <= 0:
        return None
    return abs(price - level) / level * 100.0


def evaluate(
    ticker: str,
    bars: Sequence[Bar],
    rules: Rules,
    armed: Sequence[float],
    *,
    watchlist: str = "core",
) -> list[Trigger]:
    """Evaluate every rule against the latest bar. Bars are oldest-first;
    the last element is the bar being evaluated. Returns triggers in a fixed
    order: ma_proximity/ma_cross per period in `rules.ma_periods`, then
    range_break, then armed_level per level in `armed`, then rsi_extreme.
    """
    if not bars:
        return []

    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    volumes = [b.v for b in bars]

    current = bars[-1]
    price = current.c
    bar_timestamp = current.t
    previous_close = bars[-2].c if len(bars) >= 2 else None

    current_rsi = rsi(closes, rules.rsi_period)
    current_volume_ratio = volume_ratio(volumes, rules.volume_period)

    triggers: list[Trigger] = []

    for period in rules.ma_periods:
        ma = sma(closes, period)
        if ma is None:
            continue

        if rules.ma_cross_enabled and previous_close is not None:
            crossed = (previous_close < ma <= price) or (previous_close > ma >= price)
            if crossed:
                cross_distance = _distance_pct(price, ma)
                if cross_distance is not None:
                    triggers.append(
                        Trigger(
                            ticker=ticker,
                            rule="ma_cross",
                            level=ma,
                            price=price,
                            distance_pct=cross_distance,
                            volume_ratio=current_volume_ratio,
                            rsi=current_rsi,
                            bar_timestamp=bar_timestamp,
                            watchlist=watchlist,
                        )
                    )
                continue

        ma_distance = _distance_pct(price, ma)
        if ma_distance is not None and ma_distance <= rules.ma_proximity_pct:
            triggers.append(
                Trigger(
                    ticker=ticker,
                    rule="ma_proximity",
                    level=ma,
                    price=price,
                    distance_pct=ma_distance,
                    volume_ratio=current_volume_ratio,
                    rsi=current_rsi,
                    bar_timestamp=bar_timestamp,
                    watchlist=watchlist,
                )
            )

    prior_count = len(bars) - 1
    if prior_count >= rules.range_break_days:
        prior_highs = highs[:-1][-rules.range_break_days :]
        prior_lows = lows[:-1][-rules.range_break_days :]
        confirmed = (
            current_volume_ratio is not None and current_volume_ratio >= rules.volume_multiple
        )
        if confirmed:
            prior_high = max(prior_highs)
            prior_low = min(prior_lows)
            level = None
            if price > prior_high:
                level = prior_high
            elif price < prior_low:
                level = prior_low
            if level is not None:
                range_distance = _distance_pct(price, level)
                if range_distance is not None:
                    triggers.append(
                        Trigger(
                            ticker=ticker,
                            rule="range_break",
                            level=level,
                            price=price,
                            distance_pct=range_distance,
                            volume_ratio=current_volume_ratio,
                            rsi=current_rsi,
                            bar_timestamp=bar_timestamp,
                            watchlist=watchlist,
                        )
                    )

    if previous_close is not None:
        for level in armed:
            if level <= 0:
                # `armed` is caller data (--armed CLI floats today, a DB
                # value in a later phase), not something computed here --
                # a non-positive level cannot produce a meaningful distance,
                # so it is skipped rather than crashing the whole ticker.
                continue
            crossed = (previous_close < level <= price) or (previous_close > level >= price)
            if crossed:
                triggers.append(
                    Trigger(
                        ticker=ticker,
                        rule="armed_level",
                        level=level,
                        price=price,
                        distance_pct=_distance_pct(price, level),
                        volume_ratio=current_volume_ratio,
                        rsi=current_rsi,
                        bar_timestamp=bar_timestamp,
                        watchlist=watchlist,
                    )
                )

    if current_rsi is not None:
        extreme_level = None
        if current_rsi >= rules.rsi_extreme_overbought:
            extreme_level = rules.rsi_extreme_overbought
        elif current_rsi <= rules.rsi_extreme_oversold:
            extreme_level = rules.rsi_extreme_oversold
        if extreme_level is not None:
            triggers.append(
                Trigger(
                    ticker=ticker,
                    rule="rsi_extreme",
                    level=extreme_level,
                    price=price,
                    distance_pct=_distance_pct(current_rsi, extreme_level),
                    volume_ratio=current_volume_ratio,
                    rsi=current_rsi,
                    bar_timestamp=bar_timestamp,
                    watchlist=watchlist,
                )
            )

    return triggers
