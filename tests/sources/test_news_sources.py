"""News, earnings, filings, macro — parsing and the intraday guard.

The guard is the point of these tests. **Intraday runs fetch bars only**
(docs/SPEC.md §4.2): Finnhub bills one call per symbol and 26 polls a session
would burn the day's quota for information that does not change that fast. The
guard has to raise *before* any request goes out, so each test asserts the call
list is empty too.

Everything else here is parsing, checked against recorded response shapes.

See docs/IMPLEMENTATION_PLAN.md Task 2.3.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from sources import Earnings, Filing, MacroEvent, NewsItem, edgar, finnhub, fred
from sources import intraday_run, marketaux


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class Recorder:
    """Replays payloads in order (repeating the last) and records every call."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        payload = self.payloads[index]
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)


# --------------------------------------------------------------------------
# The intraday guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name, call",
    [
        ("finnhub", lambda: finnhub.company_news(["GOOGL"], since=date(2026, 8, 1))),
        ("finnhub", lambda: finnhub.earnings_calendar(7)),
        ("marketaux", lambda: marketaux.ticker_news(["GOOGL"])),
        ("edgar", lambda: edgar.recent_filings(["GOOGL"])),
        ("fred", lambda: fred.macro_calendar(7)),
    ],
)
def test_news_sources_refuse_to_run_inside_an_intraday_context(monkeypatch, module_name, call):
    module = {"finnhub": finnhub, "marketaux": marketaux, "edgar": edgar, "fred": fred}[module_name]
    recorder = Recorder([{}])
    monkeypatch.setattr(module.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    monkeypatch.setenv("MARKETAUX_API_KEY", "k")
    monkeypatch.setenv("FRED_API_KEY", "k")
    monkeypatch.setenv("EDGAR_USER_AGENT", "market-agent test@example.com")

    with intraday_run():
        with pytest.raises(RuntimeError, match="intraday"):
            call()

    assert recorder.calls == [], "the guard must raise before any request is sent"


def test_the_intraday_flag_is_restored_after_the_context_exits(monkeypatch):
    recorder = Recorder([{"earningsCalendar": []}])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    with intraday_run():
        pass

    assert finnhub.earnings_calendar(7) == []


# --------------------------------------------------------------------------
# Finnhub
# --------------------------------------------------------------------------


def test_company_news_parses_articles_and_converts_epoch_to_utc(monkeypatch):
    recorder = Recorder(
        [
            [
                {
                    "headline": "Alphabet beats",
                    "source": "Reuters",
                    "url": "https://example.com/a",
                    "datetime": 1785000000,
                    "summary": "s",
                },
                {"headline": "", "datetime": 1785000001},  # headline-less: dropped
            ]
        ]
    )
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    result = finnhub.company_news(["GOOGL"], since=date(2026, 8, 1), until=date(2026, 8, 6), pace=0)

    assert list(result) == ["GOOGL"]
    item = result["GOOGL"][0]
    assert isinstance(item, NewsItem)
    assert item.headline == "Alphabet beats"
    assert item.published_at.endswith("Z")
    assert len(result["GOOGL"]) == 1
    assert recorder.calls[0]["params"]["from"] == "2026-08-01"
    assert recorder.calls[0]["params"]["to"] == "2026-08-06"


def test_the_finnhub_token_travels_as_a_header_not_in_the_url(monkeypatch):
    recorder = Recorder([[]])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "super-secret-token")

    finnhub.company_news(["GOOGL"], since=date(2026, 8, 1), pace=0)

    call = recorder.calls[0]
    assert call["headers"]["X-Finnhub-Token"] == "super-secret-token"
    assert "super-secret-token" not in json.dumps(call["params"])


def test_company_news_costs_one_call_per_ticker(monkeypatch):
    recorder = Recorder([[]])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    finnhub.company_news(["GOOGL", "NVDA", "AVGO"], since=date(2026, 8, 1), pace=0)

    assert len(recorder.calls) == 3


def test_earnings_calendar_filters_to_the_requested_tickers(monkeypatch):
    recorder = Recorder(
        [
            {
                "earningsCalendar": [
                    {"symbol": "GOOGL", "date": "2026-08-10", "hour": "amc", "epsEstimate": 2.1},
                    {"symbol": "AAPL", "date": "2026-08-11", "hour": "bmo"},
                ]
            }
        ]
    )
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    events = finnhub.earnings_calendar(7, tickers=["GOOGL"])

    assert events == [Earnings(ticker="GOOGL", date="2026-08-10", when="amc", eps_estimate=2.1)]
    assert len(recorder.calls) == 1


def test_finnhub_http_error_message_carries_no_token(monkeypatch):
    monkeypatch.setattr(finnhub.requests, "get", Recorder([FakeResponse({}, status_code=403)]))
    monkeypatch.setenv("FINNHUB_API_KEY", "super-secret-token")

    with pytest.raises(RuntimeError) as excinfo:
        finnhub.earnings_calendar(7)
    assert "super-secret-token" not in str(excinfo.value)
    assert "403" in str(excinfo.value)


def test_a_rate_limited_symbol_is_retried_rather_than_killing_the_sweep(monkeypatch):
    recorder = Recorder([FakeResponse({}, status_code=429), {"earningsCalendar": []}])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setattr("sources.time.sleep", lambda _: None)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    assert finnhub.earnings_calendar(7) == []
    assert len(recorder.calls) == 2


def test_a_provider_that_stays_down_still_fails_the_run(monkeypatch):
    recorder = Recorder([FakeResponse({}, status_code=503)])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.setattr("sources.time.sleep", lambda _: None)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    with pytest.raises(RuntimeError, match="503"):
        finnhub.earnings_calendar(7)
    assert len(recorder.calls) == 3


def test_missing_finnhub_key_raises_before_any_request(monkeypatch):
    recorder = Recorder([[]])
    monkeypatch.setattr(finnhub.requests, "get", recorder)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        finnhub.company_news(["GOOGL"], since=date(2026, 8, 1), pace=0)
    assert recorder.calls == []


# --------------------------------------------------------------------------
# Marketaux
# --------------------------------------------------------------------------


def test_ticker_news_maps_articles_onto_their_entities(monkeypatch):
    recorder = Recorder(
        [
            {
                "data": [
                    {
                        "title": "Chips rally",
                        "source": "example.com",
                        "url": "https://example.com/x",
                        "published_at": "2026-08-05T12:00:00.000000Z",
                        "description": "d",
                        "entities": [{"symbol": "NVDA"}, {"symbol": "UNWANTED"}],
                    }
                ]
            }
        ]
    )
    monkeypatch.setattr(marketaux.requests, "get", recorder)
    monkeypatch.setenv("MARKETAUX_API_KEY", "k")

    result = marketaux.ticker_news(["GOOGL", "NVDA"])

    assert result["GOOGL"] == []
    assert len(result["NVDA"]) == 1
    assert result["NVDA"][0].headline == "Chips rally"
    assert "UNWANTED" not in result


def test_marketaux_reports_quota_exhaustion_sent_as_http_200(monkeypatch):
    monkeypatch.setattr(
        marketaux.requests,
        "get",
        Recorder([{"error": {"code": "usage_limit_reached", "message": "no"}}]),
    )
    monkeypatch.setenv("MARKETAUX_API_KEY", "k")

    with pytest.raises(RuntimeError, match="usage_limit_reached"):
        marketaux.ticker_news(["GOOGL"])


# --------------------------------------------------------------------------
# EDGAR
# --------------------------------------------------------------------------


@pytest.fixture
def edgar_api(monkeypatch):
    recorder = Recorder(
        [
            {"0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}},
            {
                "filings": {
                    "recent": {
                        "form": ["8-K", "10-Q", "4"],
                        "filingDate": ["2026-08-05", "2026-08-01", "2026-06-01"],
                        "accessionNumber": [
                            "0001652044-26-000100",
                            "0001652044-26-000090",
                            "0001652044-26-000050",
                        ],
                    }
                }
            },
        ]
    )
    monkeypatch.setattr(edgar.requests, "get", recorder)
    monkeypatch.setenv("EDGAR_USER_AGENT", "market-agent test@example.com")
    monkeypatch.setattr(edgar, "_ticker_to_cik", None)
    return recorder


def test_recent_filings_stops_at_the_cutoff_date(edgar_api):
    result = edgar.recent_filings(["GOOGL"], since=date(2026, 8, 1), pace=0)

    assert [f.form for f in result["GOOGL"]] == ["8-K", "10-Q"]
    assert isinstance(result["GOOGL"][0], Filing)
    assert result["GOOGL"][0].url.startswith("https://www.sec.gov/Archives/edgar/data/1652044/")


def test_recent_filings_sends_the_required_user_agent(edgar_api):
    edgar.recent_filings(["GOOGL"], since=date(2026, 8, 1), pace=0)

    assert edgar_api.calls[0]["headers"]["User-Agent"] == "market-agent test@example.com"


def test_an_unmapped_ticker_is_empty_rather_than_fatal(edgar_api):
    result = edgar.recent_filings(["GOOGL", "NOTREAL"], since=date(2026, 8, 1), pace=0)

    assert result["NOTREAL"] == []
    assert len(result["GOOGL"]) == 2


def test_edgar_without_a_user_agent_raises_before_any_request(monkeypatch):
    recorder = Recorder([{}])
    monkeypatch.setattr(edgar.requests, "get", recorder)
    monkeypatch.setattr(edgar, "_ticker_to_cik", None)
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)

    with pytest.raises(RuntimeError, match="EDGAR_USER_AGENT"):
        edgar.recent_filings(["GOOGL"])
    assert recorder.calls == []


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------


def test_macro_calendar_keeps_watched_releases_only_and_sorts_by_date(monkeypatch):
    recorder = Recorder(
        [
            {
                "release_dates": [
                    {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-08-12"},
                    {"release_id": 50, "release_name": "Employment Situation", "date": "2026-08-07"},
                    {"release_id": 99, "release_name": "Wheat Outlook", "date": "2026-08-08"},
                ]
            }
        ]
    )
    monkeypatch.setattr(fred.requests, "get", recorder)
    monkeypatch.setenv("FRED_API_KEY", "k")

    events = fred.macro_calendar(14)

    assert [e.name for e in events] == ["Employment Situation", "Consumer Price Index"]
    assert isinstance(events[0], MacroEvent)


def test_dates_within_narrows_to_the_suppressor_window():
    events = [
        MacroEvent(date="2026-08-06", name="CPI"),
        MacroEvent(date="2026-08-20", name="FOMC"),
    ]

    assert [e.name for e in fred.dates_within(events, days=1, today=date(2026, 8, 6))] == ["CPI"]


def test_the_edgar_ticker_map_is_guarded_like_every_other_non_bars_fetch(monkeypatch):
    recorder = Recorder([{}])
    monkeypatch.setattr(edgar.requests, "get", recorder)
    monkeypatch.setattr(edgar, "_ticker_to_cik", None)
    monkeypatch.setenv("EDGAR_USER_AGENT", "market-agent test@example.com")

    with intraday_run():
        with pytest.raises(RuntimeError, match="intraday"):
            edgar.ticker_map()
    assert recorder.calls == []


def test_a_truncated_macro_calendar_is_paged_rather_than_silently_short(monkeypatch):
    """The watched-list filter runs client-side, so a server-side row cap drops
    the LATEST dates under an ascending sort — an absent suppressor."""
    page1 = {
        "release_dates": [
            {"release_id": 1, "release_name": "Wheat Outlook", "date": "2026-08-07"},
            {"release_id": 2, "release_name": "Cattle on Feed", "date": "2026-08-08"},
        ]
    }
    page2 = {
        "release_dates": [
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-08-12"}
        ]
    }
    recorder = Recorder([page1, page2])
    monkeypatch.setattr(fred.requests, "get", recorder)
    monkeypatch.setattr(fred, "PAGE_LIMIT", 2)
    monkeypatch.setenv("FRED_API_KEY", "k")

    events = fred.macro_calendar(30)

    assert [e.name for e in events] == ["Consumer Price Index"]
    assert [c["params"]["offset"] for c in recorder.calls] == ["0", "2"]


def test_the_watched_release_list_comes_from_config_not_code(monkeypatch):
    watched = fred.watched_releases()

    assert "consumer price index" in watched
    assert all(term == term.lower() for term in watched)


def test_macro_calendar_rejects_a_non_positive_window(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "k")

    with pytest.raises(ValueError):
        fred.macro_calendar(0)
