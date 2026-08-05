"""Data sources, plus the record types and the run-context guard they share.

**Intraday runs fetch bars only.** Finnhub bills one call per symbol, so a full
news sweep over ~100 names costs about two minutes of wall time — 26 polls a
session would burn the quota and the Actions minutes for information that does
not change every fifteen minutes (docs/SPEC.md §4.2). News is fetched exactly
twice a day, in the two digest runs.

That rule is enforced here rather than remembered: an intraday job wraps its
work in `intraday_run()`, and every news, earnings, filing, or macro fetch calls
`forbid_intraday()` first. A guard is cheaper than rediscovering a quota burn
from a month of empty digests.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# Thread-local rather than a module global: the flag describes the run, and a
# future threaded fetch must not let one worker's context silence another's guard.
_state = threading.local()


def in_intraday_run() -> bool:
    return getattr(_state, "intraday", False)


@contextmanager
def intraday_run() -> Iterator[None]:
    """Mark the enclosing work as an intraday poll. Bars only inside here."""
    previous = in_intraday_run()
    _state.intraday = True
    try:
        yield
    finally:
        _state.intraday = previous


def forbid_intraday(what: str) -> None:
    """Raise if called inside an intraday run. Called by every non-bars fetch."""
    if in_intraday_run():
        raise RuntimeError(
            f"{what} must not be fetched during an intraday run: "
            "intraday fetches bars only (docs/SPEC.md §4.2)"
        )


@dataclass(frozen=True)
class NewsItem:
    """One article. `published_at` is RFC3339 UTC, as with every timestamp here —
    the message states when the news happened, not when the scan ran."""

    ticker: str
    headline: str
    source: str
    url: str
    published_at: str
    summary: str = ""


@dataclass(frozen=True)
class Earnings:
    """A scheduled earnings date. `when` is Finnhub's `hour`: bmo | amc | dmh | ''."""

    ticker: str
    date: str
    when: str = ""
    eps_estimate: float | None = None


@dataclass(frozen=True)
class Filing:
    """One SEC filing. `form` is the raw form type (8-K, 10-Q, 4, ...)."""

    ticker: str
    form: str
    filed_on: str
    accession: str
    url: str


@dataclass(frozen=True)
class MacroEvent:
    """A scheduled macro release (CPI, FOMC, payrolls, ...)."""

    date: str
    name: str
    release_id: str = ""
