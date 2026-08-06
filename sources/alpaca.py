"""Alpaca market data: daily and intraday bars, SIP feed, free tier.

This is the only module that fetches price bars. It returns `engine.triggers.Bar`
objects so nothing downstream ever handles a raw API dict.

Four properties matter more than anything else here:

* **`end` is never closer than the SIP delay.** Free consolidated data is only
  served for windows ending at least 15 minutes in the past; a 15-minute-exact
  `end` intermittently 403s against clock skew, so the delay comes from
  `config/rules.yml` (`data.sip_delay_minutes`).
* **Both endpoints send the same `adjustment`** (`data.bar_adjustment`). Daily
  bars on a split-adjusted basis compared against intraday bars on a raw basis
  are wrong by the split factor, and nothing raises — the armed level either
  fires spuriously or never fires at all.
* **`next_page_token` is followed until exhausted, but the loop is bounded.**
  A dropped page is a permanent hole in the stored history; a token that never
  advances is an unbounded request loop inside a six-hour Actions job. Both are
  guarded.
* **Symbols are de-duplicated before batching.** The watchlist is hand-edited,
  and a symbol appearing in two batches would have its bars appended twice —
  a series of the right length holding half as many distinct sessions, each
  doubled, with every indicator over it quietly wrong.

Credentials come from the environment (`ALPACA_API_KEY_ID`,
`ALPACA_API_SECRET_KEY`) and travel as headers, never in the query string:
a request URL reaches logs and exception text, a header does not.

See docs/SPEC.md Section 4.2 and docs/IMPLEMENTATION_PLAN.md Task 2.1.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import requests  # noqa: F401 — imported so tests can patch the shared module attribute

from engine.triggers import Bar
from sources import get_json, rules_config

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
TIMEOUT = 30

# Alpaca accepts a long comma-separated `symbols` list, but the URL is not
# unbounded. 100 keeps every request well inside any server-side limit while
# still being one request for the whole core watchlist.
SYMBOL_BATCH_SIZE = 100

# Alpaca's own per-request cap on returned bars.
PAGE_LIMIT = 10000

# 250 bars for 100 symbols is 3 pages. 50 is far beyond any legitimate need and
# still bounds a pathological loop at a few seconds rather than six hours.
MAX_PAGES = 50

DAILY_TIMEFRAME = "1Day"
INTRADAY_TIMEFRAME = "1Min"


def sip_delay_minutes() -> int:
    """`data.sip_delay_minutes` from config/rules.yml. No threshold lives in code."""
    return int(rules_config()["data"]["sip_delay_minutes"])


def bar_adjustment() -> str:
    """`data.bar_adjustment` — the price basis, identical for every request."""
    return str(rules_config()["data"]["bar_adjustment"])


def intraday_poll_minutes() -> int:
    """`data.intraday_poll_minutes` — the intraday window, matching the cron."""
    return int(rules_config()["data"]["intraday_poll_minutes"])


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_end() -> datetime:
    """The latest `end` the free SIP feed will serve."""
    return datetime.now(UTC) - timedelta(minutes=sip_delay_minutes())


def _credentials() -> dict[str, str]:
    key = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not (key and secret):
        raise RuntimeError(
            "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set; refusing to call the API"
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _unique(tickers: list[str]) -> list[str]:
    """De-duplicate, preserving order. `config/watchlist_core.yml` is hand-edited."""
    seen: set[str] = set()
    return [t for t in tickers if not (t in seen or seen.add(t))]


def _chunks(tickers: list[str], size: int) -> list[list[str]]:
    return [tickers[i : i + size] for i in range(0, len(tickers), size)]


def _fetch(params: dict[str, str], headers: dict[str, str]) -> dict[str, list[dict]]:
    """One symbol batch, all pages, as raw bar dicts keyed by symbol."""
    collected: dict[str, list[dict]] = {}
    page_token: str | None = None
    seen_tokens: set[str] = set()

    for _ in range(MAX_PAGES):
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token

        body = get_json("Alpaca", BARS_URL, params=page_params, headers=headers, timeout=TIMEOUT)
        for symbol, bars in (body.get("bars") or {}).items():
            collected.setdefault(symbol, []).extend(bars)

        page_token = body.get("next_page_token")
        if not page_token:
            return collected
        if page_token in seen_tokens:
            raise RuntimeError("Alpaca returned a repeated next_page_token; refusing to loop")
        seen_tokens.add(page_token)

    raise RuntimeError(f"Alpaca pagination exceeded {MAX_PAGES} pages; refusing to loop")


def _to_bars(
    raw: dict[str, list[dict]], tickers: list[str], limit: int | None
) -> dict[str, list[Bar]]:
    """De-duplicate by timestamp, sort oldest-first, trim to the last `limit`
    bars, and include every requested ticker — a symbol Alpaca returned nothing
    for gets an empty list rather than a missing key, so callers cannot silently
    skip it."""
    result: dict[str, list[Bar]] = {}
    for ticker in tickers:
        by_ts = {bar["t"]: bar for bar in raw.get(ticker, [])}
        bars = [by_ts[ts] for ts in sorted(by_ts)]
        if limit is not None:
            bars = bars[-limit:]
        result[ticker] = [Bar.from_api(b) for b in bars]
    return result


def _bars(
    tickers: list[str], timeframe: str, start: datetime, end: datetime, limit: int | None
) -> dict[str, list[Bar]]:
    headers = _credentials()
    unique = _unique(list(tickers))

    raw: dict[str, list[dict]] = {}
    for batch in _chunks(unique, SYMBOL_BATCH_SIZE):
        params = {
            "symbols": ",".join(batch),
            "timeframe": timeframe,
            "feed": "sip",
            "start": _stamp(start),
            "end": _stamp(end),
            "limit": str(PAGE_LIMIT),
            "adjustment": bar_adjustment(),
        }
        for symbol, bars in _fetch(params, headers).items():
            raw.setdefault(symbol, []).extend(bars)

    return _to_bars(raw, unique, limit=limit)


def daily_bars(tickers: list[str], days: int) -> dict[str, list[Bar]]:
    """The last `days` daily bars per ticker, oldest-first.

    `days` counts *trading* sessions. The requested window is widened to
    calendar days (7/5) plus 45 days of slack, then trimmed back to `days` bars
    per symbol. The slack is generous on purpose: 7/5 alone lands within one or
    two bars of the target once NYSE holidays are subtracted, so a holiday
    falling inside the window would return 249 bars and fail the backfill's
    completeness check for no diagnosable reason. Price quota is not the
    constraint here (docs/SPEC.md §4.2), so a wider window is free.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    end = _window_end()
    start = end - timedelta(days=int(days * 7 / 5) + 45)
    return _bars(tickers, DAILY_TIMEFRAME, start, end, limit=days)


def intraday_bars(tickers: list[str], minutes: int | None = None) -> dict[str, list[Bar]]:
    """The last `minutes` of 1-minute bars per ticker, oldest-first.

    The window still ends at the SIP delay: "intraday" here means recent, not
    live. docs/SPEC.md Section 4.3 — the alert states the bar's timestamp, so a
    delayed window is honest rather than hidden.

    **These bars are not daily bars.** They must never be written to the `bars`
    table under the daily timeframe (`sources.store.upsert_bars` requires the
    timeframe explicitly for exactly this reason), and they must never be fed
    to `engine.triggers.evaluate` as if they were a daily series: RSI(14) over
    fifteen one-minute closes returns a number, and that number is noise that
    would consume a slot of the three-alert ceiling.
    """
    window = intraday_poll_minutes() if minutes is None else minutes
    if window <= 0:
        raise ValueError(f"minutes must be positive, got {window}")

    end = _window_end()
    return _bars(tickers, INTRADAY_TIMEFRAME, end - timedelta(minutes=window), end, limit=None)
