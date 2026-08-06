"""The last hop: a rendered message to the Telegram Bot API.

Two things here are load-bearing.

**Escaping is a delivery concern, not a formatting one.** Alerts are mostly
numbers, and MarkdownV2 reserves `.`, `-`, `(`, `)` and `!`. An unescaped
`350.00` does not render badly — Telegram answers HTTP 400 `can't parse
entities` and the alert is never delivered. `render/template.py` therefore calls
`escape_md` on every dynamic value it interpolates and supplies its own markup.

**The token is read from the environment and never passed as an argument.**
Session transcripts are permanent on-disk logs (docs/SPEC.md §9), and a
`requests` exception quotes the request URL with the token in its path, so every
string derived from an exception goes through `scripts._redact.redact` before it
is printed.

`send` returns a bool rather than raising. A digest that fails to deliver must
leave a visible failure in the job log without aborting the state commit that
follows it — a crash here would lose the run's bars as well as its message.

See docs/SPEC.md §6 and docs/IMPLEMENTATION_PLAN.md Task 3.1.
"""

from __future__ import annotations

import os

import requests

# `scripts/` rather than a module of its own: redaction was built in Phase 0 for
# the verification scripts and there is exactly one correct implementation of
# it. A second copy here would be the one that goes stale when a new key is
# added to config/.env.example.
from scripts._redact import redact

API_BASE = "https://api.telegram.org"
TIMEOUT = 20

# https://core.telegram.org/bots/api#markdownv2-style. The backslash is handled
# separately and deliberately first — see `escape_md`.
MARKDOWN_V2_SPECIALS = "_*[]()~`>#+-=|{}.!"

# Telegram's hard per-message limit. A digest on a busy day can exceed it, and
# the API rejects the whole message rather than truncating.
MAX_MESSAGE_CHARS = 4096


def escape_md(value) -> str:
    """Escape `value` for Telegram MarkdownV2.

    The backslash is escaped first. Escaping it afterwards would also escape the
    backslashes this function just inserted, turning an escaped dot into a
    literal `\\.` in the delivered text.

    Non-strings are accepted because renderers interpolate floats and ints
    directly; a `TypeError` raised inside the 17:15 scheduled run would cost a
    digest to save nothing.
    """
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\\", "\\\\")
    for char in MARKDOWN_V2_SPECIALS:
        text = text.replace(char, "\\" + char)
    return text


def _credentials(to_test_chat: bool) -> tuple[str, str]:
    """`(token, chat_id)` from the environment, or `("", "")` if incomplete.

    `to_test_chat` routes to `TELEGRAM_TEST_CHAT_ID` for dry runs
    (`scripts/verify_pipeline.py`, Phase 4). It falls back to the live chat so
    the check stays runnable before a separate test chat exists — the same
    fallback `scripts/verify_delivery.py` uses.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = ""
    if to_test_chat:
        chat_id = os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip()
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "", ""
    return token, chat_id


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split `text` into chunks within `limit`, breaking on line boundaries.

    Splitting mid-line would cut a message between a value and its escape
    backslash, which Telegram rejects. A single line longer than `limit` is a
    renderer bug rather than a normal input, so it is hard-cut as a last resort
    rather than dropped.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send(text: str, dry_run: bool = False, to_test_chat: bool = False) -> bool:
    """Deliver `text`, already escaped by the caller. True when every chunk landed.

    `dry_run` prints the message and touches no network, so a job can be run
    end to end on a laptop without a real alert reaching the phone.
    """
    if dry_run:
        print("--- DRY RUN, not sent ---")
        print(text)
        print("--- end ---")
        return True

    token, chat_id = _credentials(to_test_chat)
    if not token or not chat_id:
        print("FAIL - TELEGRAM_BOT_TOKEN or chat id not set; message not sent")
        return False

    for chunk in split_message(text):
        try:
            response = requests.post(
                f"{API_BASE}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "MarkdownV2"},
                timeout=TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — report, never abort the run
            print(f"FAIL - telegram send: {redact(f'{type(exc).__name__}: {exc}')}")
            return False

        body = {}
        if response.headers.get("content-type", "").startswith("application/json"):
            body = response.json()

        if not (response.ok and body.get("ok")):
            reason = body.get("description") or response.text[:200]
            print(f"FAIL - telegram HTTP {response.status_code}: {redact(reason)}")
            if "parse entities" in str(reason):
                print("  A parse error means a dynamic value reached the message")
                print("  without escape_md. The message is lost, not merely ugly.")
            return False

    return True
