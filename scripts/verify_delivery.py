"""Send one real message to Telegram and confirm it was delivered.

`verify_quotas.py` calls `getMe`, which proves only that the bot token
authenticates. It says nothing about whether a message reaches a human — a valid
token with a wrong chat ID, a bot the user never pressed Start on, or a chat the
bot was later removed from all pass `getMe` and deliver nothing. Delivery is the
property that matters, so it gets its own check.

This is deliberately not `verify_pipeline.py` (Phase 4, Task 4.2), which runs the
whole fetch → engine → rank → render → Telegram chain. This script verifies the
last hop alone, so that when the pipeline dry run eventually fails there is an
existing way to tell a delivery fault from an engine fault.

Sends to TELEGRAM_TEST_CHAT_ID when set, falling back to TELEGRAM_CHAT_ID. The
message is clearly labelled a test so it is never mistaken for a live alert.

Run:  python scripts/verify_delivery.py
Exit: 0 message delivered, 1 not delivered

See docs/SPEC.md §12 and docs/IMPLEMENTATION_PLAN.md deploy checklist.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._dotenv import load_env  # noqa: E402
from scripts._redact import redact  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 20


def build_message(now: datetime) -> str:
    """The test message. Labelled so it cannot be read as a live alert."""
    return (
        "✅ market-agent delivery test\n"
        f"{now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        "This is a verification message from scripts/verify_delivery.py. "
        "It is not an alert and contains no market data."
    )


def main() -> int:
    # A Telegram display name is arbitrary Unicode and the Windows console is
    # cp1252, which raises UnicodeEncodeError on a non-Latin name rather than
    # degrading. Hit on 2026-08-05 reading getUpdates.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_env(REPO_ROOT / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    # The test chat is optional by design; falling back keeps the check runnable
    # before a separate dry-run chat exists.
    chat_id = os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip()
    target = "TELEGRAM_TEST_CHAT_ID"
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        target = "TELEGRAM_CHAT_ID (no test chat set)"

    if not token or not chat_id:
        missing = " and ".join(
            n
            for n, v in (("TELEGRAM_BOT_TOKEN", token), ("chat id", chat_id))
            if not v
        )
        print(f"SKIP - {missing} not set")
        return 1

    print(f"Sending to {target}")

    try:
        # The token is a path segment, so it must never be echoed — printed URLs
        # below say <TOKEN> in its place.
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": build_message(datetime.now(UTC))},
            timeout=TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — report, never crash
        # A requests exception quotes the request URL, and the token is a path
        # segment of it. Without redaction a timeout prints a live credential.
        print(f"FAIL - {redact(f'{type(exc).__name__}: {exc}')}")
        return 1

    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    if r.ok and body.get("ok"):
        result = body["result"]
        chat = result.get("chat", {})
        name = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
        print(f"OK - HTTP {r.status_code}, message_id={result.get('message_id')}")
        print(f"     delivered to {chat.get('type')} chat @{name}")
        print("\n  Confirm the message actually appeared in Telegram. A 200 here means")
        print("  Telegram accepted it, which is one step short of you having read it.")
        return 0

    # Telegram puts the real reason in `description`; the status code alone is
    # rarely enough to tell a bad chat id from a blocked bot.
    print(f"FAIL - HTTP {r.status_code}: {redact(body.get('description', r.text[:200]))}")
    if body.get("error_code") == 400:
        print("  A 400 here is usually a wrong chat id, or a bot you have not")
        print("  pressed Start on. Message the bot, then re-read getUpdates.")
    if body.get("error_code") == 403:
        # Seen on 2026-08-05: the chat id had been filled in with the bot's own id,
        # copied from getMe. getMe answers regardless, so verify_quotas.py passed and
        # only an actual send caught it.
        if "to the bot" in body.get("description", ""):
            print("  The chat id is the BOT's own id, not yours. getMe returns the bot's")
            print("  id and it is easy to copy by mistake. Message the bot from your own")
            print("  account, then read the chat id from getUpdates.")
        else:
            print("  A 403 means the bot was blocked or removed from the chat.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
