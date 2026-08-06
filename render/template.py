"""Computed evidence -> the message a human reads. Deterministic, no network.

**This module is the permanent fallback, not scaffolding.** Phase 5 puts Gemini
in front of it, but every exit from that path — rejection by the validator,
quota exhaustion, a timeout, any exception — lands back here. It must therefore
stay complete enough to ship on its own, and it must never be deleted.

Three rules it enforces rather than merely follows:

* **The timestamp is the bar's.** `_bar_time` formats `Trigger.bar_timestamp`
  in ET and never reads a clock. A message that stated the send time would be
  claiming freshness the data does not have (docs/SPEC.md §4.3).
* **The word "buy" appears in no output.** `_assert_no_recommendation` raises on
  agent-generated text, so the constraint fails loudly in tests rather than
  quietly in a delivered alert. Third-party headlines are filtered instead of
  asserted on — see `_usable_news`.
* **Everything dynamic is escaped.** Output goes straight to
  `delivery.telegram.send` with `parse_mode=MarkdownV2`; an unescaped `.` in a
  price is not a cosmetic problem, it is an HTTP 400 and an undelivered alert.

See docs/SPEC.md §6.2 and docs/IMPLEMENTATION_PLAN.md Task 3.2.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from delivery.telegram import escape_md
from engine.suppressors import Suppressed
from sources import Earnings, MacroEvent, NewsItem

MARKET_TZ = ZoneInfo("America/New_York")

# Human wording per rule. `Trigger.detail` supplies the variant ("150-day",
# "20-session high"), so these read as a sentence once joined.
RULE_PHRASES = {
    "ma_proximity": "near its {detail} average",
    "ma_cross": "crossed its {detail} average",
    "range_break": "broke its {detail}",
    "armed_level": "touched an armed level, {detail}",
    "rsi_extreme": "RSI {detail}",
}

# Standalone recommendation words. Substring matching would flag "buyer" and
# "sell-off" in ordinary prose, so the boundary is part of the pattern.
RECOMMENDATION_RE = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)

# Digest section caps. A digest that scrolls is a digest nobody reads.
MAX_NEWS_LINES = 8
MAX_EVENT_LINES = 8


class RecommendationLeak(AssertionError):
    """Agent-generated text contained a recommendation word.

    A test failure, never a runtime condition: every string this can fire on is
    written in this file or derived from a rule name.
    """


def _assert_no_recommendation(text: str) -> str:
    if RECOMMENDATION_RE.search(text):
        raise RecommendationLeak(
            "agent-generated text contains a recommendation word; "
            "this system reports levels and never advises (docs/SPEC.md §6)"
        )
    return text


def _bar_time(bar_timestamp: str) -> str:
    """Format an RFC3339 UTC bar timestamp in ET.

    A daily bar is stamped at midnight ET, so rendering "00:00 ET" beside it
    would be noise dressed as precision; the date alone is the honest reading.
    An unparseable timestamp is passed through rather than raising — a wrong
    format should degrade the wording, not lose the alert.
    """
    try:
        moment = datetime.fromisoformat(bar_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return bar_timestamp
    local = moment.astimezone(MARKET_TZ)
    if (local.hour, local.minute) == (0, 0):
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M ET")


def _number(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _headline(item: Suppressed) -> str:
    trigger = item.trigger
    phrase = RULE_PHRASES.get(trigger.rule, trigger.rule.replace("_", " "))
    detail = trigger.detail or "no detail"
    return _assert_no_recommendation(f"${trigger.ticker} — {phrase.format(detail=detail)}")


def render_alert(item: Suppressed) -> str:
    """One trigger as the SPEC §6.2 message. Returns escaped MarkdownV2."""
    trigger = item.trigger
    lines = [
        f"*{escape_md(_headline(item))}*",
        f"Level: {escape_md(_number(trigger.level))}    "
        f"Price: {escape_md(_number(trigger.price))}",
        f"Bar: {escape_md(_bar_time(trigger.bar_timestamp))} "
        f"{escape_md('(15-min delayed SIP)')}",
        f"Distance: {escape_md(_number(trigger.distance_pct))}%",
        # The literal text is escaped too, not only the interpolated number:
        # MarkdownV2 reserves `-`, and an unescaped one anywhere in the message
        # is an HTTP 400 for the whole send, not a cosmetic problem.
        f"Volume: {escape_md(_number(trigger.volume_ratio, 1))}x {escape_md('20-day average')}",
        f"RSI\\(14\\): {escape_md(_number(trigger.rsi, 1))}",
    ]
    if item.demoted and item.reason:
        lines.append(f"Caution: {escape_md(item.reason)}")
    lines.append(f"Watchlist: {escape_md(trigger.watchlist)}")
    return "\n".join(lines)


def _usable_news(news: Sequence[NewsItem]) -> tuple[list[NewsItem], int]:
    """Split headlines into those that may be quoted and a count of the rest.

    A headline announcing a rating change is precisely the recommendation
    language the no-"buy" constraint exists to keep out of this system's output.
    It is withheld rather than reworded — rewording someone else's quote is
    worse than omitting it — and the count is reported so the omission is
    visible rather than silent.
    """
    keep = [n for n in news if not RECOMMENDATION_RE.search(n.headline)]
    return keep, len(news) - len(keep)


def render_digest(
    items: Sequence[Suppressed],
    dropped: Sequence[dict],
    *,
    day=None,
    title: str = "Digest",
    news: Sequence[NewsItem] = (),
    earnings: Sequence[Earnings] = (),
    macro: Sequence[MacroEvent] = (),
) -> str:
    """A whole run as one message. Returns escaped MarkdownV2.

    `dropped` is the `dropped_alerts` rows from `sources.store.dropped_on`, not
    the in-process list from `engine.ranking.rank` — the intraday job that
    dropped them is a separate cron process and cannot hand anything to this one
    (docs/SPEC.md §6.3).
    """
    header = f"*{escape_md(_assert_no_recommendation(title))}*"
    if day is not None:
        header += f" — {escape_md(day.isoformat())}"
    blocks = [header]

    if items:
        blocks.extend(render_alert(item) for item in items)
    else:
        blocks.append(escape_md("No triggers fired. Levels unchanged."))

    if dropped:
        lines = [escape_md(f"Held back at the daily ceiling ({len(dropped)}):")]
        for row in dropped:
            rule = str(row.get("rule", "")).replace("_", " ")
            level = row.get("level")
            suffix = "" if level is None else f" at {_number(float(level))}"
            lines.append(escape_md(f"- ${row.get('ticker', '?')} {rule}{suffix}"))
        blocks.append("_" + "\n".join(lines) + "_")

    usable, withheld = _usable_news(news)
    if usable or withheld:
        lines = [escape_md("Headlines:")]
        for article in usable[:MAX_NEWS_LINES]:
            lines.append(f"- [{escape_md(article.headline)}]({escape_md(article.url)})")
        if withheld:
            lines.append(
                escape_md(
                    f"({withheld} headline(s) withheld: they carry a rating recommendation.)"
                )
            )
        blocks.append("\n".join(lines))

    event_lines = [escape_md(f"- {e.ticker} earnings {e.date} {e.when}".rstrip()) for e in earnings]
    event_lines += [escape_md(f"- {m.date} {m.name}") for m in macro]
    if event_lines:
        blocks.append("\n".join([escape_md("Ahead:")] + event_lines[:MAX_EVENT_LINES]))

    return "\n\n".join(blocks)
