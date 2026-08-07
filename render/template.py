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
from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from delivery.telegram import escape_md, escape_md_url
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

# Recommendation words and anything built on them. The leading word boundary
# keeps "rebuy" and "oversell" out of it, but the trailing `\w*` is deliberate:
# the constraint is that the word "buy" appears in no output, and "Thinking of
# Buying Tesla Stock" carries it as plainly as "upgraded to Buy" does. A live
# dry run on 2026-08-06 surfaced exactly that headline against a `\b...\b`
# pattern. The cost is that "buyback" is withheld too; that is the right side to
# err on for a system that must never read as advice.
RECOMMENDATION_RE = re.compile(r"\b(buy|sell)\w*", re.IGNORECASE)

# Digest section caps. A digest that scrolls is a digest nobody reads.
MAX_NEWS_LINES = 8
MAX_EVENT_LINES = 8

# A list bullet, pre-escaped. `-` is reserved in MarkdownV2, and the headline
# lines are built by interpolation rather than passed whole to `escape_md`, so
# the bullet has to arrive already escaped. Writing a bare "- " there put an
# unescaped `-` in a live digest on 2026-08-06 and Telegram rejected the entire
# message with HTTP 400.
BULLET = escape_md("-")


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
        #
        # The unit is dropped along with the number when there is no number:
        # "Volume: n/ax 20-day average" reads as a glitch, and an alert that
        # looks glitchy gets trusted less than one that says plainly it does not
        # know. Intraday alerts always take this branch — volume over a
        # 15-minute window has no 20-day baseline to be a multiple of.
        f"Volume: {escape_md(_number(trigger.volume_ratio, 1))}"
        + ("" if trigger.volume_ratio is None else f"x {escape_md('20-day average')}"),
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


def _newest_first(news: Sequence[NewsItem]) -> list[NewsItem]:
    """Most recently published first, so the cap keeps the freshest headlines.

    In file order the cap keeps whichever ticker the watchlist happens to list
    first and shows nothing at all for the others.
    """
    return sorted(news, key=lambda n: n.published_at, reverse=True)


def _next_per_release(macro: Sequence[MacroEvent]) -> list[MacroEvent]:
    """The soonest date per release name, date-ascending.

    FRED publishes an FOMC row for most days in a window, so the raw calendar
    fills the whole section with one release and pushes CPI and payrolls out of
    the cap. The user needs the next occurrence of each, not every occurrence of
    the noisiest.
    """
    soonest: dict[str, MacroEvent] = {}
    for event in sorted(macro, key=lambda m: m.date):
        soonest.setdefault(event.name, event)
    return sorted(soonest.values(), key=lambda m: m.date)


def render_digest(
    items: Sequence[Suppressed],
    dropped: Sequence[dict],
    *,
    day=None,
    title: str = "Digest",
    news: Sequence[NewsItem] = (),
    earnings: Sequence[Earnings] = (),
    macro: Sequence[MacroEvent] = (),
    render_item: Callable[[Suppressed], str] = None,
) -> str:
    """A whole run as one message. Returns escaped MarkdownV2.

    `dropped` is the `dropped_alerts` rows from `sources.store.dropped_on`, not
    the in-process list from `engine.ranking.rank` — the intraday job that
    dropped them is a separate cron process and cannot hand anything to this one
    (docs/SPEC.md §6.3).

    `render_item` renders one trigger's block and defaults to `render_alert`.
    Phase 5 passes `render.narrate` through it so digest entries carry prose.
    It is a parameter rather than an import because this module is the fallback
    for the thing that would be imported: a dependency from here to the Gemini
    client would mean a provider outage could take down the path that exists to
    survive a provider outage.
    """
    render_item = render_item or render_alert
    header = f"*{escape_md(_assert_no_recommendation(title))}*"
    if day is not None:
        header += f" — {escape_md(day.isoformat())}"
    blocks = [header]

    if items:
        blocks.extend(render_item(item) for item in items)
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
        for article in _newest_first(usable)[:MAX_NEWS_LINES]:
            # The ticker is carried on the line: the sections above are ordered
            # by rule strength, so by the time the reader reaches the headlines
            # there is nothing telling them which name each one belongs to.
            lines.append(
                f"{BULLET} ${escape_md(article.ticker)} "
                f"[{escape_md(article.headline)}]({escape_md_url(article.url)})"
            )
        if withheld:
            lines.append(
                escape_md(
                    f"({withheld} headline(s) withheld: they carry a rating recommendation.)"
                )
            )
        blocks.append("\n".join(lines))

    event_lines = [
        escape_md(f"- {e.ticker} earnings {e.date} {e.when}".rstrip()) for e in earnings
    ]
    event_lines += [escape_md(f"- {m.date} {m.name}") for m in _next_per_release(macro)]
    if event_lines:
        blocks.append("\n".join([escape_md("Ahead:")] + event_lines[:MAX_EVENT_LINES]))

    return "\n\n".join(blocks)
