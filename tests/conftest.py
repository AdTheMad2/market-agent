"""Suite-wide guarantees. Currently one: the tests do not call Gemini.

Phase 5 wired `render.narrate` into both jobs, and `narrate` builds its own
`GeminiLLM` when none is injected. On a machine with `GEMINI_API_KEY` exported —
which is every machine this project is actually developed on, since `.env` is
how the verification scripts are run — every job test would start making live
model calls: slow, billed, non-deterministic, and green either way, because
`narrate` swallows provider failures by design.

That last part is what makes this worth a fixture rather than a convention. The
failure mode is not a red test. It is a suite that quietly costs money and
quietly depends on a network, and that nobody notices because it still passes.

Tests that want to exercise the model path inject a stub `LLM` explicitly; see
`tests/render/test_narrate.py`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_live_model_calls(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
