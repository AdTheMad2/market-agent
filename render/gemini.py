"""Gemini Flash, phrasing an evidence packet and nothing else.

The model receives the packet as JSON and is told to add no facts. It is not
trusted to obey that — `render/validator.py` checks the result against the same
packet, and `render/__init__.py` ships the template when the check fails. This
module's job is narrow: make one HTTP call, return text or `None`, and never
let a provider problem become the caller's problem.

Two things learned probing this API in Phase 0 and kept here:

* **The model id is not negotiable from the listing.** `gemini-2.5-flash` 404s
  for keys created after mid-2026 while still appearing in ListModels, so the
  listing is not a statement of what a given key may call (docs/RISKS.md §4).
* **Thinking tokens are billed against `maxOutputTokens`.** A small budget
  returns HTTP 200 with 74 thought tokens and no text at all, which reads as a
  model that declined to answer rather than as a budget that was too small.

The key travels in a header, never a query string, so it cannot reach a request
URL that an exception might print (docs/SPEC.md §9).

See docs/IMPLEMENTATION_PLAN.md Task 5.3.
"""

from __future__ import annotations

import json
import os

import requests  # noqa: F401 — imported so tests can patch the module attribute

MODEL = "gemini-3.6-flash"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT = 20

# Generous enough that thinking tokens do not eat the answer, small enough that
# the model cannot write an essay. Phase 0 measured 74 thought tokens on a
# three-word reply.
MAX_OUTPUT_TOKENS = 512

SYSTEM_PROMPT = """\
You rephrase a market observation for a human reader. You are given a JSON
packet of already-computed evidence.

Write one or two plain sentences describing what the packet says. Then stop.

Rules, in order of importance:

1. Introduce no number that is not in the packet. Not an approximation, not a
   percentage you derived, not a round figure "for readability". If you want to
   say a number, copy it.
2. Name no company or ticker that is not in the packet.
3. Give no recommendation, outlook, or opinion. Do not say what the reader
   should do, what is likely to happen next, or whether this is good or bad.
   The words "buy" and "sell" must not appear.
4. State no date or time other than the packet's bar timestamp.
5. Add no context you were not given — no history, no comparison to other
   companies, no explanation of why the move happened.

The packet's fields mean:

- `rule` is what fired. `ma_proximity`: the price is near a moving average.
  `ma_cross`: it crossed one. `range_break`: it passed a recent high or low.
  `rsi_extreme`: the relative strength index is at an extreme. `armed_level`:
  a price level the reader asked to be told about was touched.
- `detail` names the variant, e.g. "150-day" or "20-session high".
- `level` is the price being compared against; `price` is where it traded.
- `distance_pct` is how far apart those are, as a percentage.
- `bar_timestamp` is when the data is from. It is 15-minute-delayed data, so it
  is not the current moment and you must not imply that it is.
- `demoted` with a `suppression_reason` means the observation stands but comes
  with a caveat worth mentioning in your sentence.
- `news` are headlines about this company. You may refer to one. Do not
  editorialise about it.

Your sentence is checked against the packet automatically. Any number, ticker,
or date not found there causes your entire answer to be discarded and replaced
by a plain template. There is no partial credit and nothing to be gained by
guessing.
"""


class GeminiLLM:
    """One HTTP call per alert. Returns `None` rather than raising, wherever
    returning `None` is honest — an absent key is a configuration state, not an
    exceptional one, and Phase 0 found this provider intermittently
    unavailable."""

    def __init__(self, *, api_key: str | None = None, timeout: int = TIMEOUT):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        self._timeout = timeout

    def phrase(self, packet: dict) -> str | None:
        if not self._key:
            return None

        response = requests.post(
            ENDPOINT,
            headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [
                    {"parts": [{"text": json.dumps(packet, sort_keys=True)}]}
                ],
                "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
            },
            timeout=self._timeout,
        )
        if not response.ok:
            # Including 429. A quota-exhausted day degrades to the template and
            # is not worth a retry inside a job that has a market to keep up
            # with; docs/RISKS.md treats Gemini's availability as unreliable by
            # default rather than as an incident.
            return None

        candidates = response.json().get("candidates") or []
        if not candidates:
            return None
        # Thought parts precede the answer part; the reply is always last.
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return None
        return (parts[-1].get("text") or "").strip() or None
