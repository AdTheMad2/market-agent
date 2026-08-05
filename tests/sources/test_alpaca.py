"""Alpaca bars source, tested against a recorded response — never the live API.

The live probe lives in `scripts/verify_quotas.py` and is the only thing that
may spend quota. These tests assert the two properties that silently cost real
money or real data if they regress:

1. every request carries `feed=sip` and an `end` at least 15 minutes in the
   past — a shorter delay 403s on the free tier;
2. `next_page_token` is followed until exhausted — a dropped page is a hole in
   the history that no later run refills.

See docs/IMPLEMENTATION_PLAN.md Task 2.1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.triggers import Bar
from sources import alpaca

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class RecordedApi:
    """Replays the two recorded pages and records every request made."""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        index = min(len(self.calls) - 1, len(self.pages) - 1)
        return FakeResponse(self.pages[index])


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def recorded(monkeypatch):
    api = RecordedApi([load("alpaca_bars_page1.json"), load("alpaca_bars_page2.json")])
    monkeypatch.setattr(alpaca.requests, "get", api)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    return api


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_daily_bars_requests_sip_feed(recorded):
    alpaca.daily_bars(["GOOGL", "NVDA"], days=250)

    for call in recorded.calls:
        assert call["params"]["feed"] == "sip"


def test_daily_bars_end_is_at_least_fifteen_minutes_in_the_past(recorded):
    before = datetime.now(UTC)
    alpaca.daily_bars(["GOOGL"], days=250)

    for call in recorded.calls:
        end = parse_ts(call["params"]["end"])
        assert before - end >= timedelta(minutes=15)


def test_daily_bars_sends_credentials_as_headers_not_query(recorded):
    alpaca.daily_bars(["GOOGL"], days=250)

    call = recorded.calls[0]
    assert call["headers"]["APCA-API-KEY-ID"] == "test-key-id"
    assert call["headers"]["APCA-API-SECRET-KEY"] == "test-secret"
    assert "test-secret" not in json.dumps(call["params"])


def test_daily_bars_batches_symbols_into_one_request(recorded):
    alpaca.daily_bars(["GOOGL", "NVDA"], days=250)

    assert recorded.calls[0]["params"]["symbols"] == "GOOGL,NVDA"


def test_daily_bars_follows_next_page_token_until_exhausted(recorded):
    result = alpaca.daily_bars(["GOOGL", "NVDA"], days=250)

    assert len(recorded.calls) == 2
    assert recorded.calls[1]["params"]["page_token"] == "PAGE2"
    assert "page_token" not in recorded.calls[0]["params"]
    assert [b.t for b in result["GOOGL"]] == [
        "2026-07-27T04:00:00Z",
        "2026-07-28T04:00:00Z",
        "2026-07-29T04:00:00Z",
    ]
    assert len(result["NVDA"]) == 2


def test_daily_bars_returns_bar_objects_oldest_first(recorded):
    result = alpaca.daily_bars(["GOOGL"], days=250)

    bars = result["GOOGL"]
    assert all(isinstance(b, Bar) for b in bars)
    assert [b.t for b in bars] == sorted(b.t for b in bars)
    assert bars[-1].c == 335.76


def test_daily_bars_trims_to_the_requested_history(recorded):
    result = alpaca.daily_bars(["GOOGL"], days=2)

    assert [b.t for b in result["GOOGL"]] == [
        "2026-07-28T04:00:00Z",
        "2026-07-29T04:00:00Z",
    ]


def test_daily_bars_returns_an_empty_list_for_a_symbol_with_no_bars(recorded):
    result = alpaca.daily_bars(["GOOGL", "TSLA"], days=250)

    assert result["TSLA"] == []


def test_intraday_bars_uses_a_minute_timeframe_and_a_window_of_the_requested_length(recorded):
    alpaca.intraday_bars(["GOOGL"], minutes=15)

    params = recorded.calls[0]["params"]
    assert params["timeframe"] == "1Min"
    assert params["feed"] == "sip"
    window = parse_ts(params["end"]) - parse_ts(params["start"])
    assert window == timedelta(minutes=15)


def test_intraday_bars_end_is_at_least_fifteen_minutes_in_the_past(recorded):
    before = datetime.now(UTC)
    alpaca.intraday_bars(["GOOGL"])

    end = parse_ts(recorded.calls[0]["params"]["end"])
    assert before - end >= timedelta(minutes=15)


def test_daily_bars_chunks_symbol_lists_over_the_batch_limit(monkeypatch):
    api = RecordedApi([{"bars": {}, "next_page_token": None}])
    monkeypatch.setattr(alpaca.requests, "get", api)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")

    tickers = [f"T{i}" for i in range(alpaca.SYMBOL_BATCH_SIZE + 1)]
    alpaca.daily_bars(tickers, days=10)

    assert len(api.calls) == 2
    assert api.calls[1]["params"]["symbols"] == tickers[-1]


def test_missing_credentials_raise_rather_than_calling_the_api(monkeypatch):
    api = RecordedApi([{"bars": {}, "next_page_token": None}])
    monkeypatch.setattr(alpaca.requests, "get", api)
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError):
        alpaca.daily_bars(["GOOGL"], days=10)
    assert api.calls == []


def test_an_http_error_raises_without_the_secret_in_the_message(monkeypatch):
    def failing(url, params=None, headers=None, timeout=None):
        return FakeResponse({"message": "forbidden"}, status_code=403)

    monkeypatch.setattr(alpaca.requests, "get", failing)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "super-secret-value")

    with pytest.raises(RuntimeError) as excinfo:
        alpaca.daily_bars(["GOOGL"], days=10)
    assert "super-secret-value" not in str(excinfo.value)
    assert "403" in str(excinfo.value)
