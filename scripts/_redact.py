"""Strip secret values out of text before it is printed.

The verification scripts are careful never to print a key deliberately. That is
not enough. A `requests` exception embeds the full request URL in its message:

    ConnectionError: HTTPSConnectionPool(host='api.telegram.org', port=443):
    Max retries exceeded with url: /bot8997619195:AA.../getMe

Telegram puts its token in the path, and FRED and Marketaux put their keys in the
query string, so any network exception in any of those probes prints a live
credential. `verify_quotas.py` output is pasted into docs/RISKS.md by design, and
this repository is intended to be public, so the leak path ends in a public
commit.

Redaction is therefore applied to every string derived from a response or an
exception, not only to the ones known to contain a URL — the whole point is that
the dangerous strings are the ones nobody expected to contain a secret.

See docs/SPEC.md §9.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / "config" / ".env.example"

# Matches verify_secrets.MIN_SCANNABLE_LENGTH. Below this a value is not a
# credential, and redacting it would blank out unrelated text: a chat id of "12"
# would turn every "12" in an error message into <REDACTED>.
MIN_REDACTABLE_LENGTH = 8


def secret_names(env_example: Path = ENV_EXAMPLE) -> list[str]:
    """Names declared in .env.example. Read from the file so a new key added
    there is redacted without anyone remembering to update this module."""
    names: list[str] = []
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.append(stripped.split("=", 1)[0].strip())
    return names


def live_secrets(names: Iterable[str] | None = None) -> dict[str, str]:
    """The subset of `names` currently set to a value long enough to be a secret."""
    if names is None:
        names = secret_names()
    found = {}
    for name in names:
        value = os.environ.get(name, "").strip()
        if len(value) >= MIN_REDACTABLE_LENGTH:
            found[name] = value
    return found


def redact(text: str, secrets: dict[str, str] | None = None) -> str:
    """Replace every live secret value in `text` with a named placeholder.

    The placeholder names the variable rather than blanking it, so an error stays
    diagnosable: "<REDACTED:TELEGRAM_BOT_TOKEN>" tells you which credential the
    failing request used without disclosing it.

    Longest values are replaced first. A short secret that happens to be a
    substring of a longer one must not carve the longer one into fragments that
    then fail to match.
    """
    if not text:
        return text
    if secrets is None:
        secrets = live_secrets()
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        if value and value in text:
            text = text.replace(value, f"<REDACTED:{name}>")
    return text
