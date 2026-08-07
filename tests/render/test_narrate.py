"""`narrate` is the only part of Phase 5 an alert actually passes through.

Every test here is a way the model can fail, and every one of them has the same
required outcome: the alert still ships. Prose is the enhancement; the numbers
are the product. A Phase 5 that can prevent a Phase 3 alert from arriving is a
regression however good its sentences are.
"""

from __future__ import annotations

import pytest

from engine.suppressors import Suppressed
from engine.triggers import Trigger
from render import narrate
from render.template import render_alert


@pytest.fixture
def item() -> Suppressed:
    return Suppressed(
        trigger=Trigger(
            ticker="GOOG",
            rule="ma_proximity",
            level=100.0,
            price=100.4,
            distance_pct=0.4,
            volume_ratio=None,
            rsi=None,
            bar_timestamp="2026-07-29T04:00:00Z",
            watchlist="core",
            detail="150-day",
        ),
        demoted=False,
        reason=None,
    )


class Stub:
    """An LLM that says whatever the test tells it to."""

    def __init__(self, reply=None, raises=None):
        self._reply = reply
        self._raises = raises
        self.calls = 0

    def phrase(self, packet: dict) -> str | None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._reply


def test_good_prose_is_carried_into_the_message(item):
    llm = Stub("GOOG is sitting 0.4% under its 150-day average.")
    text = narrate(item, [], llm=llm)
    assert "sitting 0" in text
    assert llm.calls == 1


def test_the_evidence_block_is_present_whatever_the_model_said(item):
    """The prose leads; the machine-rendered numbers always follow it.

    The model is never the source of a number in a delivered message, even on
    the happy path. It rephrases evidence that is also printed underneath it,
    so a sentence that survives validation and is still somehow off can be
    checked by the reader against the block below it.
    """
    text = narrate(item, [], llm=Stub("GOOG is near its 150-day average."))
    assert render_alert(item) in text


def test_a_fabricated_number_ships_the_template_instead(item):
    """The Phase 5 observable check, in unit form."""
    text = narrate(item, [], llm=Stub("GOOG is 3% above its 150-day MA of 412.50."))
    assert text == render_alert(item)


def test_a_recommendation_ships_the_template_instead(item):
    text = narrate(item, [], llm=Stub("GOOG is near its average — a clear buy."))
    assert text == render_alert(item)


@pytest.mark.parametrize(
    "reply", [None, "", "   "], ids=["none", "empty", "whitespace"]
)
def test_an_empty_reply_ships_the_template(item, reply):
    assert narrate(item, [], llm=Stub(reply)) == render_alert(item)


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("GEMINI_API_KEY is not set"),
        TimeoutError("read timed out"),
        ValueError("candidates[0] missing"),
        KeyboardInterrupt(),
    ],
    ids=["no-key", "timeout", "bad-body", "not-an-exception-subclass"],
)
def test_any_failure_in_the_model_path_still_ships_the_alert(item, boom):
    """Including `KeyboardInterrupt`, which is a `BaseException` and slips past
    a bare `except Exception`. A quota-exhausted Friday afternoon is not the
    moment to discover which exception types were in scope."""
    assert narrate(item, [], llm=Stub(raises=boom)) == render_alert(item)


def test_the_prose_is_escaped_for_markdownv2(item):
    """Unescaped MarkdownV2 is not a cosmetic problem: it is HTTP 400 for the
    whole message and an alert that never arrives. Model output is the least
    predictable text in the system and the likeliest to carry a reserved
    character."""
    text = narrate(item, [], llm=Stub("GOOG sits near its 150-day average."))
    assert "150\\-day" in text


def test_the_model_never_sees_anything_but_the_packet(item):
    """The packet is the boundary. If a job could pass extra context in, the
    validator would be checking prose against a smaller world than the model
    was given, and the difference is exactly where a fabrication hides."""
    seen: list[dict] = []

    class Recorder:
        def phrase(self, packet):
            seen.append(packet)
            return "GOOG is near its 150-day average."

    narrate(item, [], llm=Recorder())
    from render.evidence import PACKET_KEYS

    assert set(seen[0]) == set(PACKET_KEYS)
