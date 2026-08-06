"""The backfill script's logic, with the provider stubbed.

The script *is* the Phase 2 observable check, so its completeness detection and
its exit codes are load-bearing: a short ticker must fail the run rather than
warn, because a hole in the history is inherited silently by every moving
average computed over it. None of that needs the network.

See docs/IMPLEMENTATION_PLAN.md Task 2.4.
"""

from __future__ import annotations

import pytest

from engine.triggers import Bar
from scripts import backfill
from sources import store


def make_bars(n: int) -> list[Bar]:
    return [
        Bar(t=f"2026-07-{day:02d}T04:00:00Z", o=1.0, h=2.0, l=0.5, c=1.5, v=100 + day)
        for day in range(1, n + 1)
    ]


@pytest.fixture
def watchlist(tmp_path):
    path = tmp_path / "watchlist_core.yml"
    path.write_text("tickers:\n  - GOOGL\n  - NVDA\n  - googl\n", encoding="utf-8")
    return path


def run(monkeypatch, tmp_path, bars_by_ticker, days=5):
    """Run `main` against a temp DB with `alpaca.daily_bars` stubbed."""
    db = tmp_path / "market.db"
    monkeypatch.setattr(backfill.alpaca, "daily_bars", lambda tickers, days: bars_by_ticker)
    monkeypatch.setattr(backfill, "load_env", lambda path: None)
    monkeypatch.setattr(
        "sys.argv", ["backfill.py", "--db", str(db), "--days", str(days)]
    )
    return backfill.main(), db


def test_core_tickers_upper_cases_and_deduplicates(watchlist):
    assert backfill.core_tickers(watchlist) == ["GOOGL", "NVDA"]


def test_history_length_comes_from_config():
    assert backfill.history_length() == 250


def test_a_complete_backfill_exits_zero_and_writes_every_ticker(monkeypatch, tmp_path, watchlist):
    monkeypatch.setattr(backfill, "WATCHLIST_PATH", watchlist)

    code, db = run(monkeypatch, tmp_path, {"GOOGL": make_bars(5), "NVDA": make_bars(5)})

    assert code == 0
    assert store.bar_count(db, "GOOGL") == 5
    assert store.bar_count(db, "NVDA") == 5


def test_a_second_run_adds_no_rows(monkeypatch, tmp_path, watchlist):
    """The Phase 2 observable check, in miniature."""
    monkeypatch.setattr(backfill, "WATCHLIST_PATH", watchlist)
    bars = {"GOOGL": make_bars(5), "NVDA": make_bars(5)}

    run(monkeypatch, tmp_path, bars)
    db = tmp_path / "market.db"
    before = backfill.total_rows(db)
    run(monkeypatch, tmp_path, bars)

    assert backfill.total_rows(db) == before == 10


def test_a_short_ticker_fails_the_run(monkeypatch, tmp_path, watchlist):
    monkeypatch.setattr(backfill, "WATCHLIST_PATH", watchlist)

    code, db = run(monkeypatch, tmp_path, {"GOOGL": make_bars(5), "NVDA": make_bars(3)})

    assert code == 1
    assert store.bar_count(db, "NVDA") == 3


def test_history_beyond_the_retention_length_is_pruned(monkeypatch, tmp_path, watchlist):
    monkeypatch.setattr(backfill, "WATCHLIST_PATH", watchlist)
    db = tmp_path / "market.db"
    store.init_db(db)
    store.upsert_bars(db, "GOOGL", make_bars(20))

    code, _ = run(monkeypatch, tmp_path, {"GOOGL": make_bars(5), "NVDA": make_bars(5)}, days=5)

    assert code == 0
    assert store.bar_count(db, "GOOGL") == 5


def test_an_empty_watchlist_fails_rather_than_reporting_success(monkeypatch, tmp_path):
    empty = tmp_path / "empty.yml"
    empty.write_text("tickers: []\n", encoding="utf-8")
    monkeypatch.setattr(backfill, "WATCHLIST_PATH", empty)

    code, _ = run(monkeypatch, tmp_path, {})

    assert code == 1
