"""Tests for scripts._redact.

The realistic case is the one that matters: a requests exception carrying the
full URL, which is how a token reaches a printed line without anyone printing it.
"""

from __future__ import annotations

from scripts._redact import live_secrets, redact


def test_redacts_a_token_embedded_in_an_exception_url():
    token = "8997619195:AAF_fake_token_value_for_testing_only"
    text = (
        "ConnectionError: HTTPSConnectionPool(host='api.telegram.org', port=443): "
        f"Max retries exceeded with url: /bot{token}/sendMessage"
    )
    result = redact(text, {"TELEGRAM_BOT_TOKEN": token})
    assert token not in result
    assert "<REDACTED:TELEGRAM_BOT_TOKEN>" in result
    # Still diagnosable: the failure mode survives redaction.
    assert "Max retries exceeded" in result


def test_redacts_a_key_in_a_query_string():
    key = "abcdef0123456789abcdef0123456789"
    text = f"HTTPError for url: https://api.stlouisfed.org/fred/series?api_key={key}&series_id=DFF"
    result = redact(text, {"FRED_API_KEY": key})
    assert key not in result
    assert "series_id=DFF" in result


def test_placeholder_names_the_variable_so_errors_stay_diagnosable():
    result = redact("failed with keyvalue123456", {"FINNHUB_API_KEY": "keyvalue123456"})
    assert result == "failed with <REDACTED:FINNHUB_API_KEY>"


def test_redacts_every_occurrence():
    text = "tok secretvalue123 then secretvalue123 again"
    result = redact(text, {"K": "secretvalue123"})
    assert "secretvalue123" not in result
    assert result.count("<REDACTED:K>") == 2


def test_longer_secret_is_replaced_before_a_shorter_one_inside_it():
    # A short secret that is a substring of a long one must not fragment the long
    # one and leave the remainder of it in the clear.
    long_secret = "abcdefgh12345678"
    short_secret = "abcdefgh"
    result = redact(f"url={long_secret}", {"LONG": long_secret, "SHORT": short_secret})
    assert long_secret not in result
    assert "12345678" not in result


def test_leaves_text_without_secrets_untouched():
    assert redact("HTTP 403 Forbidden", {"K": "unrelatedvalue"}) == "HTTP 403 Forbidden"


def test_empty_text_is_safe():
    assert redact("", {"K": "value123456"}) == ""


def test_short_values_are_not_redactable(monkeypatch):
    # A 2-character chat id must not turn every occurrence of those characters in
    # an error message into a placeholder.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12")
    assert "TELEGRAM_CHAT_ID" not in live_secrets()


def test_live_secrets_reads_values_from_the_environment(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "a_long_enough_value")
    assert live_secrets()["FINNHUB_API_KEY"] == "a_long_enough_value"


def test_unset_names_are_not_reported(monkeypatch):
    monkeypatch.delenv("MARKETAUX_API_KEY", raising=False)
    assert "MARKETAUX_API_KEY" not in live_secrets()
