"""Triggers + calendar context -> Suppressed records.

`engine/` is pure by design (see docs/SPEC.md Section 5.1): no network, no
filesystem, no clock reads. A suppressor may only demote or silence a
trigger; it has no code path that appends to or removes from the list, and
`apply` always returns exactly one `Suppressed` per input `Trigger`, in the
same order (docs/SPEC.md Section 5.3).

Every numeric threshold comes from `SuppressorRules`, built from the
`suppressors:` section of `config/rules.yml`. YAML loading happens in the
caller; this module only ever receives already-parsed values, including
`today`, which is passed in and never read from the clock.

See docs/SPEC.md Section 5.3 and docs/IMPLEMENTATION_PLAN.md Task 1.3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from engine.triggers import Trigger


@dataclass(frozen=True)
class SuppressorRules:
    """The `suppressors:` section of config/rules.yml, already parsed."""

    earnings_within_days: int
    ex_dividend_within_days: int
    macro_event_within_days: int

    @classmethod
    def from_mapping(cls, data: Mapping) -> "SuppressorRules":
        """Build from the full parsed config/rules.yml (reads its `suppressors:` key)."""
        suppressors = data["suppressors"]
        return cls(
            earnings_within_days=suppressors["earnings_within_days"],
            ex_dividend_within_days=suppressors["ex_dividend_within_days"],
            macro_event_within_days=suppressors["macro_event_within_days"],
        )


@dataclass(frozen=True)
class SuppressionContext:
    """Calendar context for one run, plus the thresholds to apply it with."""

    rules: SuppressorRules
    earnings_dates: Mapping[str, date]
    ex_dividend_dates: Mapping[str, date]
    macro_events: Sequence[date]
    today: date


@dataclass(frozen=True)
class Suppressed:
    """A trigger, unchanged, plus whether calendar context demotes it."""

    trigger: Trigger
    demoted: bool
    reason: str | None


def _within(event_date: date, today: date, window_days: int) -> bool:
    return abs((event_date - today).days) <= window_days


def apply(triggers: Sequence[Trigger], context: SuppressionContext) -> list[Suppressed]:
    """Demote triggers whose ticker has an earnings date, ex-dividend date,
    or whose run date is near a macro event, all within their configured
    windows. Never creates or removes a trigger; output has the same length
    and order as `triggers`.
    """
    result: list[Suppressed] = []
    for trigger in triggers:
        causes: list[str] = []

        earnings_date = context.earnings_dates.get(trigger.ticker)
        if earnings_date is not None and _within(
            earnings_date, context.today, context.rules.earnings_within_days
        ):
            causes.append(f"earnings {earnings_date.isoformat()}")

        ex_dividend_date = context.ex_dividend_dates.get(trigger.ticker)
        if ex_dividend_date is not None and _within(
            ex_dividend_date, context.today, context.rules.ex_dividend_within_days
        ):
            causes.append(f"ex-dividend {ex_dividend_date.isoformat()}")

        macro_hits = [
            event_date
            for event_date in context.macro_events
            if _within(event_date, context.today, context.rules.macro_event_within_days)
        ]
        if macro_hits:
            nearest = min(macro_hits, key=lambda d: (abs((d - context.today).days), d))
            causes.append(f"macro {nearest.isoformat()}")

        reason = "; ".join(sorted(causes)) if causes else None
        result.append(Suppressed(trigger=trigger, demoted=bool(causes), reason=reason))

    return result
