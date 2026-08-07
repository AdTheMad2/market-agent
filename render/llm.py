"""The provider interface, so `narrate` depends on a shape and not on Gemini.

There is one implementation today (`render/gemini.py`) and this file still
earns its place: it is what lets every test in `tests/render/test_narrate.py`
describe a failure mode — a timeout, an empty reply, a fabricated number —
without a network, and it is what makes replacing the provider a one-line
change if Gemini's free tier stops existing, which docs/RISKS.md treats as a
question of when.

See docs/IMPLEMENTATION_PLAN.md Task 5.3.
"""

from __future__ import annotations

from typing import Protocol


class LLM(Protocol):
    def phrase(self, packet: dict) -> str | None:
        """Rephrase `packet` as one or two sentences of prose.

        Returns `None` when the provider had nothing to give — no key, an empty
        candidate, an exhausted quota. Implementations may also raise; every
        caller in this package treats both the same way, because a `None` and a
        `ReadTimeout` cost the reader the same thing and neither may cost them
        the alert.
        """
