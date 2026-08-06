"""Data sources: the record types, the run-context guard, and shared HTTP.

**Intraday runs fetch bars only.** Finnhub bills one call per symbol, so a full
news sweep over ~100 names costs about two minutes of wall time — 26 polls a
session would burn the quota and the Actions minutes for information that does
not change every fifteen minutes (docs/SPEC.md §4.2). News is fetched exactly
twice a day, in the two digest runs.

The guard is *arming*, not enforcement: an intraday job wraps its work in
`intraday_run()` and every news, earnings, filing or macro fetch calls
`forbid_intraday()` first. A job that forgets the wrapper is not protected — the
only thing that catches that is `tests/jobs/` asserting the intraday entrypoint
enters the context, which Phase 4 owes.

Thresholds come from `config/rules.yml` via `rules_config()`; batch sizes,
timeouts and pacing stay in code as plumbing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yml"

# A ContextVar, not threading.local: a ContextVar's value is copied into tasks
# started with `asyncio` and into `ContextVar.copy_context().run(...)`, so the
# guard survives the concurrency shapes likely to be added to a news sweep.
# It does NOT propagate into a bare `threading.Thread` or a ThreadPoolExecutor
# worker — code that fans out that way must re-enter `intraday_run()` inside
# the worker, or the guard is silently off there.
_intraday: ContextVar[bool] = ContextVar("intraday_run", default=False)

_rules_cache: dict | None = None


def rules_config(refresh: bool = False) -> dict:
    """Parsed `config/rules.yml`, cached per process."""
    global _rules_cache
    if _rules_cache is None or refresh:
        _rules_cache = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return _rules_cache


def in_intraday_run() -> bool:
    return _intraday.get()


@contextmanager
def intraday_run() -> Iterator[None]:
    """Mark the enclosing work as an intraday poll. Bars only inside here."""
    token = _intraday.set(True)
    try:
        yield
    finally:
        _intraday.reset(token)


def forbid_intraday(what: str) -> None:
    """Raise if called inside an intraday run. Called by every non-bars fetch."""
    if in_intraday_run():
        raise RuntimeError(
            f"{what} must not be fetched during an intraday run: "
            "intraday fetches bars only (docs/SPEC.md §4.2)"
        )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def get_json(
    provider: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    attempts: int = MAX_ATTEMPTS,
    backoff: float = BACKOFF_SECONDS,
):
    """GET returning parsed JSON, retrying 429 and 5xx.

    A single 429 partway through a hundred-symbol sweep used to abort the whole
    job — the digest died over one transient rate-limit response. Retries are
    bounded so a provider that is genuinely down still fails the run instead of
    holding an Actions runner open.

    Raises `RuntimeError` carrying the provider and status only. Never the
    response body and never the URL: FRED, Marketaux and EDGAR-style providers
    put credentials in the query string, and a requests exception carries the
    full request. That is how a key reaches a traceback.
    """
    last_status: int | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == attempts:
                raise RuntimeError(f"{provider} request failed: {type(exc).__name__}") from None
            time.sleep(backoff * attempt)
            continue

        if response.ok:
            return response.json()

        last_status = response.status_code
        if last_status not in RETRY_STATUSES or attempt == attempts:
            raise RuntimeError(f"{provider} returned HTTP {last_status}")
        time.sleep(backoff * attempt)

    raise RuntimeError(f"{provider} returned HTTP {last_status}")


# --------------------------------------------------------------------------
# Record types
# --------------------------------------------------------------------------


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
