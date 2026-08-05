"""Marketaux: news fallback when Finnhub is unavailable or rate-limited.

Marketaux takes a comma-separated symbol list, so a whole watchlist costs a
handful of calls rather than one per name — but the free tier caps at 100
requests/day and 3 articles per response, which is why it is the fallback and
not the primary (docs/SPEC.md §4.1).

Its failure mode is unusual and worth guarding: **quota exhaustion is reported
in the response body with HTTP 200**, not as a status code. A caller that only
checks `response.ok` sees a successful empty sweep.

Never called from an intraday run: intraday fetches bars only (§4.2).

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os

import requests

from sources import NewsItem, forbid_intraday

URL = "https://api.marketaux.com/v1/news/all"
TIMEOUT = 20

# Marketaux's own cap on symbols per request.
SYMBOL_BATCH_SIZE = 50


def _token() -> str:
    key = os.environ.get("MARKETAUX_API_KEY")
    if not key:
        raise RuntimeError("MARKETAUX_API_KEY is not set; refusing to call the API")
    return key


def ticker_news(tickers: list[str], limit: int = 3) -> dict[str, list[NewsItem]]:
    """Recent articles per ticker. Every requested ticker gets a key, empty if
    Marketaux returned nothing for it — a missing key would let a caller skip a
    name without noticing."""
    forbid_intraday("Marketaux news")
    token = _token()
    result: dict[str, list[NewsItem]] = {t: [] for t in tickers}

    for start in range(0, len(tickers), SYMBOL_BATCH_SIZE):
        batch = tickers[start : start + SYMBOL_BATCH_SIZE]
        params = {
            "symbols": ",".join(batch),
            "filter_entities": "true",
            "language": "en",
            "limit": str(limit),
            "api_token": token,
        }
        try:
            response = requests.get(URL, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # The API token is a query parameter, so the exception's own text
            # carries it. Only the type name is safe.
            raise RuntimeError(f"Marketaux request failed: {type(exc).__name__}") from None
        if not response.ok:
            raise RuntimeError(f"Marketaux returned HTTP {response.status_code}")

        body = response.json()
        if "error" in body:
            # HTTP 200 with an error object is how the free tier reports
            # exhaustion. Treating it as success is a silent empty digest.
            raise RuntimeError(f"Marketaux error: {body['error'].get('code', 'unknown')}")

        for article in body.get("data", []):
            for entity in article.get("entities", []):
                symbol = entity.get("symbol")
                if symbol in result:
                    result[symbol].append(
                        NewsItem(
                            ticker=symbol,
                            headline=article.get("title", ""),
                            source=article.get("source", "marketaux"),
                            url=article.get("url", ""),
                            published_at=article.get("published_at", ""),
                            summary=article.get("description", "") or "",
                        )
                    )
    return result
