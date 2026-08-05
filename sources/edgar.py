"""SEC EDGAR: recent filings per ticker.

EDGAR needs no API key, but its fair-use policy requires a descriptive
User-Agent identifying the operator and a contact address. Requests without one
are blocked, so `EDGAR_USER_AGENT` is required rather than defaulted — a
made-up header would be a policy violation shipped in code.

Ticker-to-CIK mapping comes from `company_tickers.json`, fetched once per
process and cached: it is a ~1 MB file covering every registrant and re-fetching
it per symbol would be the rudest possible use of a free public service.

Never called from an intraday run: intraday fetches bars only (docs/SPEC.md §4.2).

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import requests

from sources import Filing, forbid_intraday

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{accession}-index.htm"
TIMEOUT = 30

_ticker_to_cik: dict[str, str] | None = None


def _headers() -> dict[str, str]:
    agent = os.environ.get("EDGAR_USER_AGENT")
    if not agent:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set; SEC fair use requires a descriptive "
            "User-Agent with a contact address"
        )
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


def _get(url: str) -> dict:
    try:
        response = requests.get(url, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"EDGAR request failed: {type(exc).__name__}") from None
    if not response.ok:
        raise RuntimeError(f"EDGAR returned HTTP {response.status_code}")
    return response.json()


def ticker_map(refresh: bool = False) -> dict[str, str]:
    """`{TICKER: zero-padded 10-digit CIK}`, fetched once per process."""
    global _ticker_to_cik
    if _ticker_to_cik is None or refresh:
        body = _get(TICKER_MAP_URL)
        _ticker_to_cik = {
            row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in body.values()
        }
    return _ticker_to_cik


def recent_filings(
    tickers: list[str], since: date | None = None, forms: tuple[str, ...] | None = None
) -> dict[str, list[Filing]]:
    """Filings per ticker filed on or after `since` (default: the last 7 days).

    A ticker EDGAR does not know — an ADR, an ETF, a symbol change — yields an
    empty list rather than raising: one unmapped name must not lose the digest
    for the other ninety-nine.
    """
    forbid_intraday("SEC filings")
    cutoff = since or (datetime.now(UTC).date() - timedelta(days=7))
    mapping = ticker_map()

    result: dict[str, list[Filing]] = {}
    for ticker in tickers:
        cik = mapping.get(ticker.upper())
        if cik is None:
            result[ticker] = []
            continue

        recent = _get(SUBMISSIONS_URL.format(cik=cik)).get("filings", {}).get("recent", {})
        rows = zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            strict=False,
        )
        filings = []
        for form, filed_on, accession in rows:
            if filed_on < cutoff.isoformat():
                # `recent` is newest-first, so the first older filing ends it.
                break
            if forms and form not in forms:
                continue
            filings.append(
                Filing(
                    ticker=ticker,
                    form=form,
                    filed_on=filed_on,
                    accession=accession,
                    url=ARCHIVE_URL.format(
                        cik_int=int(cik),
                        accession_nodash=accession.replace("-", ""),
                        accession=accession,
                    ),
                )
            )
        result[ticker] = filings
    return result
