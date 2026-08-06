"""Telegram delivery. The property that matters is that a price never breaks a message.

Every alert this system sends is mostly numbers, and MarkdownV2 treats `.`, `-`,
`(`, `)` and `!` as syntax. An unescaped `350.00` does not render badly — Telegram
rejects the whole request with HTTP 400 and the alert is never delivered at all.
So the escaping tests below are delivery tests, not formatting tests.

The second property is that the bot token is read from the environment and never
appears in an argument, a printed URL, or an exception message. See
scripts/_redact.py for why that is not paranoia.

See docs/IMPLEMENTATION_PLAN.md Task 3.1.
"""

from __future__ import annotations

import pytest

from delivery import telegram


# --------------------------------------------------------------------------
# escape_md
# --------------------------------------------------------------------------


def test_escape_md_escapes_the_dot_in_a_price():
    assert telegram.escape_md("GOOG 350.00") == "GOOG 350\\.00"


def test_escape_md_escapes_every_markdownv2_special():
    for char in "_*[]()~`>#+-=|{}.!":
        assert telegram.escape_md(char) == "\\" + char, char


def test_escape_md_leaves_plain_text_alone():
    assert telegram.escape_md("GOOG crossed its 150 day average") == (
        "GOOG crossed its 150 day average"
    )


def test_escape_md_escapes_the_backslash_itself_first():
    # A backslash escaped after the specials would double-escape them and emit
    # a literal "\\." instead of an escaped dot.
    assert telegram.escape_md("a\\b.c") == "a\\\\b\\.c"


def test_escape_md_accepts_non_strings():
    # Renderers pass floats and ints straight through; str() here beats a
    # TypeError raised inside a scheduled run at 17:15.
    assert telegram.escape_md(350.0) == "350\\.0"


def test_escaped_message_leaves_no_unbalanced_markdown():
    # The real contract: after escaping, no character is left that Telegram
    # would read as an unclosed entity.
    escaped = telegram.escape_md("$GOOG — Level: 350.00 (bar 14:15 ET) [core] 1.5x!")
    for i, char in enumerate(escaped):
        if char in telegram.MARKDOWN_V2_SPECIALS:
            assert i > 0 and escaped[i - 1] == "\\", f"unescaped {char!r} at {i}"


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:AAtest-token-value")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99887766")
    monkeypatch.delenv("TELEGRAM_TEST_CHAT_ID", raising=False)


def test_send_dry_run_makes_no_request(creds, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dry_run must not touch the network")

    monkeypatch.setattr(telegram.requests, "post", explode)
    assert telegram.send("hello", dry_run=True) is True


def test_send_posts_to_the_configured_chat(creds, monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _Response(200, {"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert telegram.send("GOOG 350\\.00") is True
    assert captured["url"].endswith("/sendMessage")
    assert captured["json"]["chat_id"] == "99887766"
    assert captured["json"]["parse_mode"] == "MarkdownV2"


def test_send_returns_false_when_credentials_are_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_TEST_CHAT_ID", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("must not attempt a send without credentials")

    monkeypatch.setattr(telegram.requests, "post", explode)
    assert telegram.send("hello") is False


def test_send_prefers_the_test_chat_when_set(creds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TEST_CHAT_ID", "111")
    captured = {}
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda url, json, timeout: (
            captured.update(json=json),
            _Response(200, {"ok": True, "result": {"message_id": 1}}),
        )[1],
    )
    assert telegram.send("hi", to_test_chat=True) is True
    assert captured["json"]["chat_id"] == "111"


def test_send_returns_false_on_a_telegram_error(creds, monkeypatch):
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda url, json, timeout: _Response(
            400, {"ok": False, "error_code": 400, "description": "can't parse entities"}
        ),
    )
    assert telegram.send("bad *markdown") is False


def test_send_never_prints_the_token(creds, monkeypatch, capsys):
    # A requests exception quotes the request URL and the token is a path
    # segment of it. This is the leak path scripts/_redact.py exists for.
    def raise_with_url(*args, **kwargs):
        raise telegram.requests.ConnectionError(
            "Max retries exceeded with url: /bot1234567890:AAtest-token-value/sendMessage"
        )

    monkeypatch.setattr(telegram.requests, "post", raise_with_url)
    assert telegram.send("hello") is False
    assert "AAtest-token-value" not in capsys.readouterr().out


def test_send_splits_a_message_over_the_length_limit(creds, monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda url, json, timeout: (
            calls.append(json["text"]),
            _Response(200, {"ok": True, "result": {"message_id": len(calls)}}),
        )[1],
    )
    long_text = "\n".join(f"line {i}" for i in range(1200))
    assert len(long_text) > telegram.MAX_MESSAGE_CHARS

    assert telegram.send(long_text) is True
    assert len(calls) > 1
    assert all(len(chunk) <= telegram.MAX_MESSAGE_CHARS for chunk in calls)


class _Response:
    """The slice of requests.Response that telegram.send actually reads."""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = str(payload)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._payload


# --------------------------------------------------------------------------
# escape_md_url
# --------------------------------------------------------------------------


def test_escape_md_url_leaves_the_url_resolvable():
    # The rules inside a link target differ from the rest of a message. Running
    # a URL through escape_md yields https://finnhub\.io/... , which 404s and
    # reads as a bad feed rather than as a rendering bug.
    url = "https://finnhub.io/api/news?id=e0404a17c6f8"
    assert telegram.escape_md_url(url) == url
    assert telegram.escape_md(url) != url


def test_escape_md_url_escapes_the_two_characters_that_matter():
    # A `)` would close the link early; a `\` would escape whatever follows it.
    assert telegram.escape_md_url("https://x.test/a)b") == r"https://x.test/a\)b"
    assert telegram.escape_md_url("https://x.test/a" + "\\" + "b") == r"https://x.test/a\\b"
