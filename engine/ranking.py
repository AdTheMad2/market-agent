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


def _sort_key(item: Suppressed) -> tuple:
    trigger = item.trigger
    volume_rank = float("inf") if trigger.volume_ratio is None else -trigger.volume_ratio
    return (
        0 if trigger.rule == "armed_level" else 1,
        1 if item.demoted else 0,
        0 if trigger.watchlist == "core" else 1,
        trigger.distance_pct,
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
    distance_pct ascending; volume_ratio descending (None is the weakest
    value); ticker then rule ascending as a final, total tiebreak.

    `remaining = max(0, ceiling - already_sent_today)`; a negative or
    over-budget `already_sent_today` yields an empty `to_send` rather than
    raising. `to_send` is the first `remaining` items of the sorted list;
    `dropped` is everything after, still in sorted order.
    """
    ordered = sorted(suppressed, key=_sort_key)
    remaining = 0 if already_sent_today < 0 else max(0, ceiling - already_sent_today)
    return ordered[:remaining], ordered[remaining:]
