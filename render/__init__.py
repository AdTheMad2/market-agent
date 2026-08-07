"""Computed evidence -> the message a human reads.

`narrate` is the only entry point a job should call. It tries the model, checks
what came back against the same packet the model saw, and ships the
deterministic template whenever anything about that goes wrong — a rejection, an
absent key, an exhausted quota, a timeout, a provider outage, an exception
nobody predicted.

**The fallback is the load-bearing part.** Phase 3 delivers alerts without any
of this, and it must keep doing so on the worst day the provider has. So the
failure path is not an error branch here; it is the default, and the model path
is an enhancement layered on top of it. A Phase 5 capable of preventing a Phase
3 alert from arriving would be a regression however good its sentences read.

**The numbers in a delivered message are never the model's.** Even when prose
survives validation, it is placed *above* the same machine-rendered evidence
block Phase 3 shipped, not instead of it. The reader can check any sentence
against the block underneath it, and the system's factual output is identical
whether the model ran or not.

See docs/IMPLEMENTATION_PLAN.md Task 5.3 and docs/SPEC.md §6.
"""

from __future__ import annotations

from collections.abc import Sequence

from delivery.telegram import escape_md
from engine.suppressors import Suppressed
from render.evidence import build_packet
from render.llm import LLM
from render.template import render_alert, render_digest
from render.validator import validate
from sources import NewsItem

__all__ = ["narrate", "render_alert", "render_digest"]


def _prose(item: Suppressed, news: Sequence[NewsItem], llm: LLM) -> str | None:
    """The model path, with every way it can fail collapsed into `None`.

    `BaseException` rather than `Exception` is deliberate and is the one place
    in this codebase that catches it. The set of things a third-party HTTP
    client raises is not knowable in advance, and the cost of guessing wrong is
    an undelivered alert during a session — the exact failure Phase 3 exists to
    make impossible.
    """
    try:
        packet = build_packet(item, news)
        prose = llm.phrase(packet)
        if not prose or not prose.strip():
            return None
        if not validate(prose, packet):
            return None
        return prose.strip()
    except BaseException:  # noqa: BLE001 — see the docstring
        return None


def narrate(
    item: Suppressed, news: Sequence[NewsItem] = (), *, llm: LLM | None = None
) -> str:
    """One trigger as a message: prose if it earned its place, evidence always.

    `llm` defaults to Gemini and is injected in tests. The import is local so
    that `render.template` — the fallback — carries no dependency on the
    provider it is the fallback for.
    """
    if llm is None:
        from render.gemini import GeminiLLM

        llm = GeminiLLM()

    evidence = render_alert(item)
    prose = _prose(item, news, llm)
    if prose is None:
        return evidence
    return f"_{escape_md(prose)}_\n\n{evidence}"
