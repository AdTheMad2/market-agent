"""The evidence packet: everything the model is allowed to know.

One packet describes one trigger. It is built from computed values only — no
prose, no framing, no adjective that was not derived from a number — and it is
the sole input to `render/gemini.py` and the sole reference for
`render/validator.py`. Those two facts are the same fact stated twice, and the
whole design of Phase 5 rests on it: the model cannot introduce a number that
the validator will not then reject, because the validator checks against this
dict and the model saw nothing else.

Two consequences worth stating, because both are easy to undo by accident:

* **A number missing here is a number the model may not use.** Adding a field
  to `Trigger` without adding it here does not merely omit it from the prompt —
  it makes any prose mentioning it fail validation and silently fall back to
  the template. `tests/render/test_evidence.py` walks `Trigger`'s fields to
  catch that.
* **A headline included here is licensed.** The validator flattens this packet,
  so every token in an included headline becomes an accepted token. That is why
  the recommendation filter runs *here* and not only in the template: the
  template's `RecommendationLeak` assertion guards text this file's output can
  bypass entirely.

See docs/IMPLEMENTATION_PLAN.md Task 5.1 and docs/SPEC.md §6.4.
"""

from __future__ import annotations

from collections.abc import Sequence

from engine.suppressors import Suppressed
from render.template import RECOMMENDATION_RE
from sources import NewsItem

# The packet's exact shape. Declared rather than inferred so that the test for
# "no key nobody computed" has something to compare against, and so a reader of
# the system prompt can see the field list without running the code.
PACKET_KEYS = (
    "ticker",
    "rule",
    "detail",
    "level",
    "price",
    "distance_pct",
    "volume_ratio",
    "rsi",
    "bar_timestamp",
    "watchlist",
    "demoted",
    "suppression_reason",
    "news",
    "headlines_withheld",
)

NEWS_KEYS = ("ticker", "headline", "source", "url", "published_at")

# Decimal places per field, matching `render/template.py` exactly.
#
# The packet must carry numbers at the precision the reader sees, not at the
# precision a float happens to have. A live run put
# "crossed its 50-day moving average level of 356.50559999999996" into a
# delivered sentence, beside a table reading 356.51 — the model had copied the
# packet faithfully, which is what it is told to do, and the validator accepted
# it, which is correct for an exact match. Neither component was wrong. The
# packet was.
PRECISION = {
    "level": 2,
    "price": 2,
    "distance_pct": 2,
    "volume_ratio": 1,
    "rsi": 1,
}


def _news_entry(item: NewsItem) -> dict:
    return {key: getattr(item, key) for key in NEWS_KEYS}


def _rounded(name: str, value: float | None) -> float | None:
    return None if value is None else round(float(value), PRECISION[name])


def build_packet(s: Suppressed, news: Sequence[NewsItem]) -> dict:
    """Every computed number, rule name, and quotable headline, JSON-safe.

    `news` is filtered twice: to this trigger's ticker, because a headline about
    another company licenses that company's name in this company's prose; and
    through `RECOMMENDATION_RE`, because a headline announcing a rating change
    is the exact language this system must never emit. The withheld count is
    carried rather than dropped, so an omission is visible in the packet the
    same way it is visible in the digest.
    """
    t = s.trigger
    for_this_ticker = [item for item in news if item.ticker == t.ticker]
    quotable = [
        item
        for item in for_this_ticker
        if not RECOMMENDATION_RE.search(item.headline)
    ]

    return {
        "ticker": t.ticker,
        "rule": t.rule,
        "detail": t.detail,
        "level": _rounded("level", t.level),
        "price": _rounded("price", t.price),
        "distance_pct": _rounded("distance_pct", t.distance_pct),
        "volume_ratio": _rounded("volume_ratio", t.volume_ratio),
        "rsi": _rounded("rsi", t.rsi),
        "bar_timestamp": t.bar_timestamp,
        "watchlist": t.watchlist,
        "demoted": s.demoted,
        "suppression_reason": s.reason,
        "news": [_news_entry(item) for item in quotable],
        "headlines_withheld": len(for_this_ticker) - len(quotable),
    }
