"""Alpaca market data: daily and intraday bars, SIP feed, free tier.

This is the only module that fetches price bars. It returns `engine.triggers.Bar`
objects so nothing downstream ever handles a raw API dict.

Two properties matter more than anything else here:

* **`end` is never closer than the SIP delay.** Free consolidated data is only
  served for windows ending at least 15 minutes in the past; a 15-minute-exact
  `end` intermittently 403s against clock skew, so the delay comes from
  `config/rules.yml` (`data.sip_delay_minutes`, currently 16) rather than code.
* **`next_page_token` is followed until exhausted.** Alpaca truncates a
  multi-symbol window across pages. A dropped page is a permanent hole in the
  stored history — later runs fetch later windows and never come back for it.

Credentials come from the environment (`ALPACA_API_KEY_ID`,
`ALPACA_API_SECRET_KEY`) and travel as headers, never in the query string:
a request URL reaches logs and exception text, a header does not.

See docs/SPEC.md Section 4.2 and docs/IMPLEMENTATION_PLAN.md Task 2.1.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
import yaml

from engine.triggers import Bar

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
TIMEOUT = 30

# Alpaca accepts a long comma-separated `symbols` list, but the URL is not
# unbounded. 100 keeps every request well inside any server-side limit while
# still being one request for the whole core watchlist.
SYMBOL_BATCH_SIZE = 100

# Alpaca's own per-request cap on returned bars.
PAGE_LIMIT = 10000

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yml"

_sip_delay_cache: int | None = None


def sip_delay_minutes() -> int:
    """`data.sip_delay_minutes` from config/rules.yml. No threshold lives in code."""
    global _sip_delay_cache
    if _sip_delay_cache is None:
        data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
        _sip_delay_cache = int(data["data"]["sip_delay_minutes"])
    return _sip_delay_cache


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


def _chunks(tickers: list[str], size: int) -> list[list[str]]:
    return [tickers[i : i + size] for i in range(0, len(tickers), size)]


def _fetch(params: dict[str, str], headers: dict[str, str]) -> dict[str, list[dict]]:
    """One symbol batch, all pages, as raw bar dicts keyed by symbol."""
    collected: dict[str, list[dict]] = {}
    page_token: str | None = None

    while True:
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token
        try:
            response = requests.get(BARS_URL, params=page_params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # A requests exception carries the full request object; its text can
            # reach a log. The status-free message below cannot.
            raise RuntimeError(f"Alpaca request failed: {type(exc).__name__}") from None
        if not response.ok:
            # Never interpolate the response body or the request URL: the body is
            # provider text and the URL is reconstructable from the params, which
            # is how a key ends up in a traceback.
            raise RuntimeError(f"Alpaca returned HTTP {response.status_code}")

        body = response.json()
        for symbol, bars in (body.get("bars") or {}).items():
            collected.setdefault(symbol, []).extend(bars)

        page_token = body.get("next_page_token")
        if not page_token:
            return collected


def _to_bars(
    raw: dict[str, list[dict]], tickers: list[str], limit: int | None
) -> dict[str, list[Bar]]:
    """Sort oldest-first, trim to the last `limit` bars, and include every
    requested ticker — a symbol Alpaca returned nothing for gets an empty list
    rather than a missing key, so callers cannot silently skip it."""
    result: dict[str, list[Bar]] = {}
    for ticker in tickers:
        bars = sorted(raw.get(ticker, []), key=lambda b: b["t"])
        if limit is not None:
            bars = bars[-limit:]
        result[ticker] = [Bar.from_api(b) for b in bars]
    return result


def daily_bars(tickers: list[str], days: int) -> dict[str, list[Bar]]:
    """The last `days` daily bars per ticker, oldest-first.

    `days` counts *trading* sessions, so the requested window is widened to
    calendar days (roughly 7/5) with a fortnight of slack for holidays, then
    trimmed back to `days` bars per symbol.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    headers = _credentials()
    end = _window_end()
    start = end - timedelta(days=int(days * 7 / 5) + 14)

    raw: dict[str, list[dict]] = {}
    for batch in _chunks(list(tickers), SYMBOL_BATCH_SIZE):
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Day",
            "feed": "sip",
            "start": _stamp(start),
            "end": _stamp(end),
            "limit": str(PAGE_LIMIT),
            "adjustment": "split",
        }
        for symbol, bars in _fetch(params, headers).items():
            raw.setdefault(symbol, []).extend(bars)

    return _to_bars(raw, list(tickers), limit=days)


def intraday_bars(tickers: list[str], minutes: int = 15) -> dict[str, list[Bar]]:
    """The last `minutes` of 1-minute bars per ticker, oldest-first.

    The window still ends at the SIP delay: "intraday" here means recent, not
    live. docs/SPEC.md Section 4.3 — the alert states the bar's timestamp, so a
    delayed window is honest rather than hidden.
    """
    if minutes <= 0:
        raise ValueError(f"minutes must be positive, got {minutes}")

    headers = _credentials()
    end = _window_end()
    start = end - timedelta(minutes=minutes)

    raw: dict[str, list[dict]] = {}
    for batch in _chunks(list(tickers), SYMBOL_BATCH_SIZE):
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Min",
            "feed": "sip",
            "start": _stamp(start),
            "end": _stamp(end),
            "limit": str(PAGE_LIMIT),
        }
        for symbol, bars in _fetch(params, headers).items():
            raw.setdefault(symbol, []).extend(bars)

    return _to_bars(raw, list(tickers), limit=None)
