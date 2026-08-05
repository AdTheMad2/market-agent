"""Tests for scripts/verify_secrets.py.

The property that matters: a real secret value sitting in a tracked file must be
found, and the value itself must never appear in the report. A leak report that
prints the leaked value turns the report into a second leak.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_secrets import (
    missing_names,
    required_names,
    scan_files_for_values,
)


def test_required_names_reads_names_and_ignores_comments(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text(
        "# a comment\n"
        "\n"
        "ALPACA_API_KEY_ID=\n"
        "# TELEGRAM_COMMENTED_OUT=\n"
        "FINNHUB_API_KEY=\n",
        encoding="utf-8",
    )
    assert required_names(example) == ["ALPACA_API_KEY_ID", "FINNHUB_API_KEY"]


def test_missing_names_reports_absent_and_blank(tmp_path):
    env = {"ALPACA_API_KEY_ID": "abc123xyz789", "FINNHUB_API_KEY": "   "}
    assert missing_names(["ALPACA_API_KEY_ID", "FINNHUB_API_KEY", "FRED_API_KEY"], env) == [
        "FINNHUB_API_KEY",
        "FRED_API_KEY",
    ]


def test_scan_finds_a_secret_value_in_a_tracked_file(tmp_path):
    leaky = tmp_path / "notes.md"
    leaky.write_text("token is PKTEST1234567890ABCDEF here", encoding="utf-8")

    findings = scan_files_for_values(
        [leaky], {"ALPACA_API_KEY_ID": "PKTEST1234567890ABCDEF"}
    )

    assert findings == [(leaky, "ALPACA_API_KEY_ID")]


def test_scan_report_never_contains_the_value(tmp_path):
    leaky = tmp_path / "notes.md"
    secret = "PKTEST1234567890ABCDEF"
    leaky.write_text(f"token is {secret} here", encoding="utf-8")

    findings = scan_files_for_values([leaky], {"ALPACA_API_KEY_ID": secret})

    assert secret not in repr(findings)


def test_scan_is_clean_when_nothing_leaks(tmp_path):
    clean = tmp_path / "notes.md"
    clean.write_text("nothing to see", encoding="utf-8")

    assert scan_files_for_values([clean], {"ALPACA_API_KEY_ID": "PKTEST1234567890ABCDEF"}) == []


def test_short_values_are_not_scanned(tmp_path):
    """A 3-character value would match half the tree and report nothing useful."""
    f = tmp_path / "code.py"
    f.write_text("x = 1", encoding="utf-8")

    assert scan_files_for_values([f], {"SOME_KEY": "1"}) == []


def test_binary_files_do_not_crash_the_scan(tmp_path):
    blob = tmp_path / "market.db"
    blob.write_bytes(b"\x00\x01\x02\xff\xfe SQLite format")

    assert scan_files_for_values([blob], {"SOME_KEY": "PKTEST1234567890ABCDEF"}) == []
