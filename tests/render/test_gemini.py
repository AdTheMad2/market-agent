"""The client's job is to return text or None, and never to raise upward.

Every case here was found by running `scripts/verify_prose.py` against the live
API on 2026-08-07, not by imagining what an API might do. The truncation case in
particular is the one that unit tests would never have suggested: it is a
successful response containing a correct-looking sentence that is simply cut in
half.
"""

from __future__ import annotations

import pytest

import render.gemini as gemini
from render.gemini import GeminiLLM

PACKET = {"ticker": "GOOG", "level": 100.0, "bar_timestamp": "2026-07-29T04:00:00Z"}


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def _reply(text: str, finish: str = "STOP") -> dict:
    return {
        "candidates": [
            {"finishReason": finish, "content": {"parts": [{"text": text}]}}
        ]
    }


@pytest.fixture
def posted(monkeypatch):
    """Capture the outgoing request and script the reply."""
    sent: list[dict] = []
    box: dict = {"response": FakeResponse(200, _reply("GOOG is near 100.00."))}

    def fake_post(url, **kwargs):
        sent.append({"url": url, **kwargs})
        return box["response"]

    monkeypatch.setattr(gemini.requests, "post", fake_post)
    return sent, box


def _llm() -> GeminiLLM:
    # pace=0: the real 4-second spacing exists for a free-tier rate limit that
    # no test is subject to, and paying it here would add seconds per test.
    return GeminiLLM(api_key="k", pace=0)


def test_a_finished_reply_comes_back_as_text(posted):
    assert _llm().phrase(PACKET) == "GOOG is near 100.00."


def test_a_truncated_reply_is_discarded(posted):
    """The live failure: HTTP 200, a sentence, and `MAX_TOKENS`.

    "As of 2026-07-29T04:00" is entirely packet-derived, so the validator
    accepts it — truncation removes content rather than inventing it. Nothing
    downstream can tell this from a finished thought, so it has to die here.
    """
    _, box = posted
    box["response"] = FakeResponse(200, _reply("As of 2026-07-29T04:00", "MAX_TOKENS"))
    assert _llm().phrase(PACKET) is None


def test_a_429_is_not_an_incident(posted):
    _, box = posted
    box["response"] = FakeResponse(429, {"error": {"message": "quota"}})
    assert _llm().phrase(PACKET) is None


@pytest.mark.parametrize(
    "body",
    [{}, {"candidates": []}, {"candidates": [{"finishReason": "STOP"}]}],
    ids=["empty-body", "no-candidates", "no-parts"],
)
def test_a_shapeless_body_is_none_not_a_keyerror(posted, body):
    _, box = posted
    box["response"] = FakeResponse(200, body)
    assert _llm().phrase(PACKET) is None


def test_no_key_means_no_call_at_all(posted, monkeypatch):
    """An absent key is a configuration state. Spending a request to be told
    so — on every entry of every digest — is the wrong shape of honest."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    sent, _ = posted
    assert GeminiLLM(pace=0).phrase(PACKET) is None
    assert sent == []


def test_the_key_never_reaches_the_url(posted):
    """A key in a query string reaches every exception message, every log line
    and every retry trace. It travels in a header (docs/SPEC.md §9)."""
    sent, _ = posted
    _llm().phrase(PACKET)
    assert "k" not in sent[0]["url"]
    assert sent[0]["headers"]["x-goog-api-key"] == "k"


def test_the_packet_is_the_entire_user_message(posted):
    """Whatever the model is told beyond the system prompt must be the packet
    and only the packet — that equality is what makes the validator's world the
    same size as the model's."""
    import json

    sent, _ = posted
    _llm().phrase(PACKET)
    body = sent[0]["json"]
    assert json.loads(body["contents"][0]["parts"][0]["text"]) == PACKET


def test_the_token_budget_leaves_room_for_thinking(posted):
    """842 thought tokens were measured on a real packet. A budget under that
    returns a fragment, and the fragment validates."""
    sent, _ = posted
    _llm().phrase(PACKET)
    assert sent[0]["json"]["generationConfig"]["maxOutputTokens"] >= 1024


def test_consecutive_calls_are_paced(posted, monkeypatch):
    slept: list[float] = []
    # The clock starts at 0.0 on purpose: `time.monotonic()`'s origin is
    # undefined, and a first reading of exactly zero is what broke the pacing
    # when "have I called yet?" was asked by truthiness.
    clock = iter([0.0, 1.0, 9.0])
    monkeypatch.setattr(gemini.time, "sleep", slept.append)
    monkeypatch.setattr(gemini.time, "monotonic", lambda: next(clock))

    llm = GeminiLLM(api_key="k", pace=4.0)
    llm.phrase(PACKET)  # first call: nothing to wait for; stores 0.0
    llm.phrase(PACKET)  # clock 1.0: one second of four elapsed, so three to wait

    assert slept == [3.0]


def test_a_long_gap_between_calls_costs_nothing(posted, monkeypatch):
    slept: list[float] = []
    clock = iter([0.0, 30.0, 30.0])
    monkeypatch.setattr(gemini.time, "sleep", slept.append)
    monkeypatch.setattr(gemini.time, "monotonic", lambda: next(clock))

    llm = GeminiLLM(api_key="k", pace=4.0)
    llm.phrase(PACKET)
    llm.phrase(PACKET)

    assert slept == []
