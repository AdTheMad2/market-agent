"""Suppressed triggers -> ordered (to_send, dropped), enforcing the daily ceiling.

`engine/` is pure by design (see docs/SPEC.md Section 5.1): no network, no
filesystem, no clock reads. `rank` never decides *whether* a trigger fired or
was demoted -- `triggers.evaluate` and `suppressors.apply` already did that --
it only orders and, once the ceiling is reached, defers the weakest ones. A
dropped item is never discarded: `to_send` and `dropped` together always
contain every input exactly once, in sorted order, so Phase 3's digest can
report what was left out.

The ceiling itself is a parameter, not a constant read from config here --
the caller reads `alerts.intraday_ceiling` from config/rules.yml and passes
it in.

See docs/SPEC.md Section 6.3 and docs/IMPLEMENTATION_PLAN.md Task 1.4.
"""

from __future__ import annotations

from collections.abc import Sequence

from engine.suppressors import Suppressed


def _urgency(trigger) -> float:
    """Rank-only urgency, distinct from `trigger.distance_pct`.

    `distance_pct` is rule-local and stays in the record for the alert text,
    but it is not commensurable across rule types: for `ma_proximity` it is a
    percentage of a *price* (typically 0-2%), while for `rsi_extreme` it is
    computed over the RSI point scale and can be 25%+. Sorting on it directly
    would bury every `rsi_extreme` trigger behind price triggers, and make a
    *more* extreme RSI reading sort *worse* than a marginal one.

    Rules that fire on a breach -- `armed_level`, `ma_cross`, `range_break`,
    `rsi_extreme` -- get urgency 0.0: the thing already happened, so there is
    no "how close" left to measure. Only `ma_proximity` fires on proximity
    rather than a breach, so it alone uses `distance_pct` as urgency: the
    closer the price sits to the average, the more urgent the watch.
    """
    return 0.0 if trigger.rule != "ma_proximity" else trigger.distance_pct


def _sort_key(item: Suppressed) -> tuple:
    """Order key, most significant first.

    `armed_level` before every other rule; not-demoted before demoted -- an
    armed level was set deliberately by the user, so even demoted it still
    outranks a clean, never-demoted trigger of a different rule (see
    test_demoted_armed_level_still_outranks_clean_non_armed); watchlist ==
    "core" before anything else; then `_urgency` ascending (see its
    docstring for why this is not `distance_pct`); volume_ratio descending
    with None as the weakest value; ticker then rule ascending as a final,
    total tiebreak.
    """
    trigger = item.trigger
    volume_rank = float("inf") if trigger.volume_ratio is None else -trigger.volume_ratio
    return (
        0 if trigger.rule == "armed_level" else 1,
        1 if item.demoted else 0,
        0 if trigger.watchlist == "core" else 1,
        _urgency(trigger),
        volume_rank,
        trigger.ticker,
        trigger.rule,
    )


def rank(
    suppressed: Sequence[Suppressed], already_sent_today: int, ceiling: int
) -> tuple[list[Suppressed], list[Suppressed]]:
    """Order `suppressed` and split it at the remaining daily budget.

    Order, most significant first: armed_level before every other rule;
    not-demoted before demoted; watchlist == "core" before anything else;
    urgency ascending (see `_urgency`); volume_ratio descending (None is the
    weakest value); ticker then rule ascending as a final, total tiebreak.

    `remaining = max(0, ceiling - already_sent_today)`; an over-budget
    `already_sent_today` (>= ceiling) yields an empty `to_send`, a legitimate
    state once enough alerts have already gone out today. A negative
    `already_sent_today` or a negative `ceiling` cannot arise from a healthy
    caller -- both are state-layer bugs (Phase 2/4) -- so they raise
    `ValueError` rather than silently producing an empty digest that looks
    like a quiet market.
    """
    if already_sent_today < 0:
        raise ValueError(f"already_sent_today must be >= 0, got {already_sent_today}")
    if ceiling < 0:
        raise ValueError(f"ceiling must be >= 0, got {ceiling}")
    ordered = sorted(suppressed, key=_sort_key)
    remaining = max(0, ceiling - already_sent_today)
    return ordered[:remaining], ordered[remaining:]
