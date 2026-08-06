"""Marketaux: news fallback when Finnhub is unavailable or rate-limited.

**Coverage warning, and the reason this is a fallback and not a source.** The
free tier returns 3 articles *per response*, not per symbol, and caps at 100
requests/day. A 50-symbol batch therefore carries news for at most 3 of those
50 names; the other 47 come back empty and are indistinguishable from "no news
today". Do not read an empty result here as an absence of news. Covering a
100-name watchlist properly would need per-symbol requests, which the daily cap
does not allow (docs/SPEC.md §4.1, RISKS.md R-2).

Its other failure mode is unusual and worth guarding: **quota exhaustion is
reported in the response body with HTTP 200**, not as a status code. A caller
that only checks `response.ok` sees a successful empty sweep.

Never called from an intraday run: intraday fetches bars only (§4.2).

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os

import requests  # noqa: F401 — imported so tests can patch the shared module attribute

from sources import NewsItem, forbid_intraday, get_json

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
    """Recent articles, mapped onto whichever requested tickers they mention.

    Every requested ticker gets a key — empty if nothing came back for it, which
    given the per-response article cap above is the common case rather than a
    statement about the news.
    """
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
        # The token is a query parameter — Marketaux offers no header form — so
        # `get_json` never surfaces the URL or the body in its error text.
        body = get_json("Marketaux", URL, params=params, timeout=TIMEOUT)

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
