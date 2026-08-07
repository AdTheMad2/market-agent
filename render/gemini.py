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
import time

import requests  # noqa: F401 — imported so tests can patch the module attribute

MODEL = "gemini-3.6-flash"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT = 20

# Thinking tokens are billed against this budget and there is no way to switch
# them off: `thinkingConfig.thinkingBudget: 0` is an HTTP 400 on this model,
# measured 2026-08-07. Phase 0 saw 74 thought tokens on a three-word reply and
# 512 looked generous; a real packet costs **842**, which left 512 producing
# either a 429 or a sentence cut off mid-timestamp — "As of 2026-07-29T04:00" —
# that the validator then accepted, because truncation removes content rather
# than inventing it. Two live failures from one number being too small.
MAX_OUTPUT_TOKENS = 2048

# Free-tier requests-per-minute is low enough that a digest's eight calls in a
# burst mostly 429. Spacing them costs a job that is already running hours
# behind GitHub's scheduler (R-13) a handful of seconds, and buys prose on
# entries that would otherwise silently fall back.
PACE_SECONDS = 4.0

# Anything other than STOP means the reply is not a finished thought. MAX_TOKENS
# is the one that matters: it returns a truncated sentence that is entirely
# packet-derived and so passes validation cleanly. A fragment is worse than no
# prose — it reads as a system that broke mid-word.
FINISHED = "STOP"

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

Two things about how it should read. Your sentence is shown directly above a
block listing every one of these numbers in a table, so:

- Do not open by restating `bar_timestamp`. The reader can see the time; a
  sentence beginning "At 2026-08-07T14:24:00Z" spends its first eight words on
  something already on screen, in a format nobody reads aloud.
- Do not use this packet's field names as English. "This observation is
  demoted" is our internal word for it. Say what the caveat is instead: that
  earnings are days away, that the level is one the reader asked about.

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

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: int = TIMEOUT,
        pace: float = PACE_SECONDS,
    ):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        self._timeout = timeout
        self._pace = pace
        # `None`, not `0.0`. `time.monotonic()`'s origin is undefined and a
        # small return value is legal, so "have I called yet?" cannot be asked
        # by truthiness — on a clock reading 0.0 the pacing silently stops
        # pacing, which is the one condition under which it is needed.
        self._last_call: float | None = None

    def _wait_turn(self) -> None:
        """Space consecutive calls. Per instance, which is per digest run —
        the object outlives the loop over a digest's entries and dies with the
        job, so there is no state here that survives a process."""
        if self._pace <= 0:
            return
        if self._last_call is not None:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._pace:
                time.sleep(self._pace - elapsed)
        self._last_call = time.monotonic()

    def phrase(self, packet: dict) -> str | None:
        if not self._key:
            return None

        self._wait_turn()
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
        if candidates[0].get("finishReason") != FINISHED:
            return None
        # Thought parts precede the answer part; the reply is always last.
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return None
        return (parts[-1].get("text") or "").strip() or None
