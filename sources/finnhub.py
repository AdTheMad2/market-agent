"""Finnhub: company news and the earnings calendar.

Finnhub bills **one call per symbol** for news, so this is the expensive source
in the system (docs/SPEC.md §4.2). Both functions therefore refuse to run inside
an intraday context: `forbid_intraday` raises rather than quietly spending the
day's quota 26 times over.

Free tier is 60 calls/minute. A sweep over ~100 names is paced so a digest run
cannot trip the limit and lose the back half of the watchlist to 429s.

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime, timedelta

import requests

from sources import Earnings, NewsItem, forbid_intraday

BASE = "https://finnhub.io/api/v1"
TIMEOUT = 20

# 60 calls/minute on the free tier. One second between symbols keeps a
# 100-name sweep inside it with no burst arithmetic to get wrong.
PACE_SECONDS = 1.0


def _token() -> str:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError("FINNHUB_API_KEY is not set; refusing to call the API")
    return key


def _get(path: str, params: dict[str, str]) -> object:
    try:
        response = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # The exception carries the request URL, and the token is a query
        # parameter. Only the type name is safe to surface.
        raise RuntimeError(f"Finnhub request failed: {type(exc).__name__}") from None
    if not response.ok:
        raise RuntimeError(f"Finnhub returned HTTP {response.status_code}")
    return response.json()


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def company_news(
    tickers: list[str], since: date, until: date | None = None, pace: float = PACE_SECONDS
) -> dict[str, list[NewsItem]]:
    """Articles per ticker published between `since` and `until` (inclusive).

    One API call per ticker — never call this from an intraday job.
    """
    forbid_intraday("company news")
    token = _token()
    end = until or datetime.now(UTC).date()

    result: dict[str, list[NewsItem]] = {}
    for index, ticker in enumerate(tickers):
        if index and pace:
            time.sleep(pace)
        raw = _get(
            "/company-news",
            {"symbol": ticker, "from": since.isoformat(), "to": end.isoformat(), "token": token},
        )
        items = raw if isinstance(raw, list) else []
        result[ticker] = [
            NewsItem(
                ticker=ticker,
                headline=item.get("headline", ""),
                source=item.get("source", "finnhub"),
                url=item.get("url", ""),
                published_at=_iso(int(item.get("datetime", 0))),
                summary=item.get("summary", ""),
            )
            for item in items
            if item.get("headline")
        ]
    return result


def earnings_calendar(days: int, tickers: list[str] | None = None) -> list[Earnings]:
    """Scheduled earnings in the next `days` days, one call for the whole window.

    `tickers` filters client-side: the calendar endpoint's symbol parameter
    returns only that symbol, so filtering here keeps the sweep at one call.
    """
    forbid_intraday("the earnings calendar")
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    token = _token()
    today = datetime.now(UTC).date()
    raw = _get(
        "/calendar/earnings",
        {"from": today.isoformat(), "to": (today + timedelta(days=days)).isoformat(), "token": token},
    )
    rows = raw.get("earningsCalendar", []) if isinstance(raw, dict) else []
    wanted = set(tickers) if tickers else None

    return [
        Earnings(
            ticker=row["symbol"],
            date=row["date"],
            when=row.get("hour", "") or "",
            eps_estimate=row.get("epsEstimate"),
        )
        for row in rows
        if row.get("symbol") and row.get("date") and (wanted is None or row["symbol"] in wanted)
    ]
