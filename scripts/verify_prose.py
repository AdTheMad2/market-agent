"""The Phase 5 observable check, against the live model.

Three questions, in the order they matter:

1. **Does the model produce prose a reader would want?** Only a live call can
   answer that, and it is the only half of Phase 5 no unit test can reach.
2. **Does a fabricated number get caught?** Answered by injecting one into an
   otherwise-real reply, so the rejection is exercised against text the model
   actually wrote rather than against a string chosen to fail.
3. **Does a broken provider still deliver an alert?** Answered by pointing the
   client at an invalid key and confirming the templated message ships whole.

Nothing is delivered and nothing is written. The script prints what would have
been sent, because the point is to read it — prose that validates but reads
badly is a Phase 5 that passed its check and failed its purpose, and only a
human can tell those apart.

Run:  python scripts/verify_prose.py [--samples 3]
Exit: 0 all three questions answered as they must be
      1 no key, no model reply at all, or a check that did not hold

See docs/IMPLEMENTATION_PLAN.md Task 5.3 Steps 3-4.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render  # noqa: E402
from engine.suppressors import Suppressed  # noqa: E402
from engine.triggers import Trigger  # noqa: E402
from render.evidence import build_packet  # noqa: E402
from render.gemini import GeminiLLM  # noqa: E402
from render.template import render_alert  # noqa: E402
from render.validator import validate  # noqa: E402
from scripts._dotenv import load_env  # noqa: E402
from sources import NewsItem  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Real shapes, fixed values. A live quote would make the output different every
# run and this script's job is to be read and compared, not to be current — the
# live half of it is the model, not the market.
SAMPLES: list[tuple[Suppressed, list[NewsItem]]] = [
    (
        Suppressed(
            trigger=Trigger(
                ticker="GOOGL", rule="ma_proximity", level=336.57, price=335.76,
                distance_pct=0.24, volume_ratio=1.4, rsi=58.2,
                bar_timestamp="2026-07-29T04:00:00Z", watchlist="core",
                detail="150-day",
            ),
            demoted=False, reason=None,
        ),
        [
            NewsItem(
                ticker="GOOGL",
                headline="Alphabet opens a data centre in Ohio",
                source="Reuters", url="https://example.com/a",
                published_at="2026-07-29T12:00:00Z",
            )
        ],
    ),
    (
        Suppressed(
            trigger=Trigger(
                ticker="NVDA", rule="armed_level", level=214.0, price=214.35,
                distance_pct=0.16, volume_ratio=None, rsi=None,
                bar_timestamp="2026-08-07T14:24:00Z", watchlist="core",
                detail="armed above",
            ),
            demoted=False, reason=None,
        ),
        [],
    ),
    # A demoted trigger: the caveat is the part most likely to be editorialised
    # into advice, which is the failure this whole phase is built around.
    (
        Suppressed(
            trigger=Trigger(
                ticker="AVGO", rule="rsi_extreme", level=70.0, price=412.88,
                distance_pct=4.6, volume_ratio=2.1, rsi=73.2,
                bar_timestamp="2026-08-06T04:00:00Z", watchlist="core",
                detail="overbought",
            ),
            demoted=True, reason="earnings within 3 days",
        ),
        [],
    ),
]


class _Fixed:
    """An `LLM` that replays a reply already received. No second request."""

    def __init__(self, reply: str):
        self._reply = reply

    def phrase(self, packet: dict) -> str:
        return self._reply


def _rule(number: int, text: str) -> None:
    print(f"\n{'=' * 72}\n{number}. {text}\n{'=' * 72}")


def _prose_samples(llm: GeminiLLM, count: int) -> tuple[int, int]:
    """Print each sample's packet, reply, verdict and message. Returns
    (replies received, replies that validated)."""
    replied = validated = 0
    for item, news in SAMPLES[:count]:
        packet = build_packet(item, news)
        trigger = item.trigger
        print(f"\n--- {trigger.ticker} {trigger.rule} ---")

        reply = llm.phrase(packet)
        if reply is None:
            print("  model: no reply (no key, truncated, empty candidate, or non-2xx)")
            continue
        replied += 1
        ok = validate(reply, packet)
        validated += int(ok)
        print(f"  model: {reply}")
        print(f"  validator: {'ACCEPT' if ok else 'REJECT'}")
        print("  would send:")
        # `_Fixed` replays the reply already in hand rather than calling again.
        # Calling twice showed a verdict from one request beside a message built
        # from another, and the two disagreed often enough to be misleading —
        # this provider's replies vary run to run and its 429s arrive at random.
        for line in render.narrate(item, news, llm=_Fixed(reply)).splitlines():
            print(f"    {line}")
    return replied, validated


class _Fabricator:
    """Wraps a real reply and appends a number the packet cannot support.

    Injecting into genuine model output rather than inventing a whole sentence
    keeps the test honest: the prose around the fabrication is exactly what the
    validator would otherwise accept, so a pass here is the fabrication being
    caught and not the sentence being odd.
    """

    def __init__(self, inner: GeminiLLM):
        self._inner = inner
        self.injected: str | None = None

    def phrase(self, packet: dict) -> str | None:
        reply = self._inner.phrase(packet)
        if reply is None:
            return None
        self.injected = f"{reply.rstrip('.')}, up 37.2% since April."
        return self.injected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=len(SAMPLES))
    args = parser.parse_args()

    # The Windows console defaults to cp1252 and mangles the em dashes this
    # script's whole purpose is to let someone read. Same reason as
    # `scripts/verify_quotas.py`.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_env(REPO_ROOT / ".env")
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set. Nothing to verify — this script's whole")
        print("purpose is the live call. Fill it into .env and re-run.")
        return 1

    llm = GeminiLLM()
    failures: list[str] = []

    _rule(1, "Do the alerts read as prose?")
    replied, validated = _prose_samples(llm, args.samples)
    print(f"\n  {replied}/{args.samples} replied, {validated}/{replied or 1} validated")
    if replied == 0:
        failures.append("the model returned nothing at all for any sample")
    elif validated == 0:
        failures.append("no reply survived the validator; the prompt or the check is wrong")

    _rule(2, "Is a fabricated number caught?")
    item, news = SAMPLES[0]
    fabricator = _Fabricator(llm)
    message = render.narrate(item, news, llm=fabricator)
    if fabricator.injected is None:
        failures.append("could not inject: the model returned nothing for sample 1")
    else:
        print(f"  injected: {fabricator.injected}")
        shipped_template = message == render_alert(item)
        print(f"  shipped the template instead: {shipped_template}")
        if not shipped_template:
            failures.append("a fabricated number reached the message")

    _rule(3, "Does an invalid key still deliver the alert?")
    broken = GeminiLLM(api_key="not-a-real-key")
    message = render.narrate(item, news, llm=broken)
    intact = message == render_alert(item)
    print(f"  templated alert shipped intact: {intact}")
    for line in message.splitlines():
        print(f"    {line}")
    if not intact:
        failures.append("an invalid key changed the delivered alert")

    print(f"\n{'=' * 72}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("All three checks held. Read the prose above before calling Phase 5 done —")
    print("a sentence can validate and still be worth deleting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
