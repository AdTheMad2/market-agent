"""The deterministic renderer. It is the permanent fallback, not scaffolding.

Phase 5 puts Gemini in front of this, but every path out of that -- rejection by
the validator, quota exhaustion, a timeout, any exception -- lands back here.
So these tests assert the contract the *user* is promised, not an intermediate
format: the bar timestamp is present and is the bar's, the word "buy" never
appears, and a dropped trigger is reported rather than silently lost.

See docs/SPEC.md §6.2 and docs/IMPLEMENTATION_PLAN.md Task 3.2.
"""

from __future__ import annotations

from datetime import date

import pytest

from engine.suppressors import Suppressed
from engine.triggers import Trigger
from render import template
from sources import Earnings, MacroEvent, NewsItem


def make_trigger(**overrides) -> Trigger:
    fields = {
        "ticker": "GOOGL",
        "rule": "ma_proximity",
        "level": 412.5,
        "price": 414.2,
        "distance_pct": 0.41,
        "volume_ratio": 1.53,
        "rsi": 58.4,
        "bar_timestamp": "2026-08-05T20:00:00Z",
        "watchlist": "core",
        "detail": "150-day",
    }
    fields.update(overrides)
    return Trigger(**fields)


def make_suppressed(demoted: bool = False, reason: str | None = None, **overrides) -> Suppressed:
    return Suppressed(trigger=make_trigger(**overrides), demoted=demoted, reason=reason)


def plain(text: str) -> str:
    """Strip MarkdownV2 escapes, for asserting on wording rather than syntax.

    `-` and `.` are both escaped, so a literal `"150-day" in text` fails against
    perfectly correct output. Escaping itself is asserted separately, on purpose.
    """
    return text.replace("\\", "")


@pytest.fixture
def item() -> Suppressed:
    return make_suppressed()


# --------------------------------------------------------------------------
# render_alert — the SPEC §6.2 contract
# --------------------------------------------------------------------------


def test_alert_states_the_bar_timestamp_not_the_send_time(item):
    # 20:00Z on 2026-08-05 is 16:00 ET; the message must carry that, and the
    # renderer must never consult a clock to produce it.
    text = template.render_alert(item)
    assert "2026" in text and "08" in text and "05" in text
    assert "16:00 ET" in text


def test_alert_says_the_data_is_delayed(item):
    assert "delayed" in template.render_alert(item)


def test_alert_never_says_buy(item):
    assert "buy" not in template.render_alert(item).lower()


def test_alert_carries_every_contract_field(item):
    text = template.render_alert(item)
    for label in ("Level", "Price", "Distance", "Volume", "RSI", "Watchlist"):
        assert label in text, label
    assert "GOOGL" in text
    assert "412" in text and "414" in text
    assert "1.5" in text.replace("\\", "")
    assert "58.4" in text.replace("\\", "")
    assert "core" in text


def test_alert_names_which_moving_average_fired(item):
    # "near a moving average, level 412.50" cannot be acted on: the reader
    # cannot tell a 50-day touch from a 200-day one.
    assert "150-day" in plain(template.render_alert(item))


def test_alert_shows_caution_only_when_demoted(item):
    assert "Caution" not in template.render_alert(item)
    demoted = make_suppressed(demoted=True, reason="earnings 2026-08-07")
    text = template.render_alert(demoted)
    assert "Caution" in text
    assert "earnings" in text


def test_alert_survives_missing_volume_and_rsi():
    text = template.render_alert(make_suppressed(volume_ratio=None, rsi=None))
    assert "n/a" in text
    assert "None" not in text


def test_alert_output_is_markdownv2_escaped(item):
    # Every literal dot must be escaped or Telegram rejects the whole message.
    text = template.render_alert(item)
    assert "\\." in text
    assert ".00 " not in text.replace("\\.", "")


def test_every_special_in_an_alert_is_escaped_except_the_bold_wrapper(item):
    # The general form of the bug: a literal like "20-day average" written
    # straight into an f-string looks harmless and 400s the entire send. Only
    # the `*` wrapping the first line is intentional markup.
    from delivery.telegram import MARKDOWN_V2_SPECIALS

    lines = template.render_alert(make_suppressed(demoted=True, reason="earnings 2026-08-07"))
    for line in lines.split("\n"):
        body = line[1:-1] if line.startswith("*") and line.endswith("*") else line
        for i, char in enumerate(body):
            if char in MARKDOWN_V2_SPECIALS:
                assert i > 0 and body[i - 1] == "\\", f"unescaped {char!r} in {line!r}"


def test_alert_escapes_a_hostile_suppressor_reason():
    text = template.render_alert(make_suppressed(demoted=True, reason="macro *2026-08-06*"))
    assert "\\*" in text


# --------------------------------------------------------------------------
# render_digest
# --------------------------------------------------------------------------


def test_digest_reports_the_dropped_count():
    dropped = [
        {"ticker": "NVDA", "rule": "rsi_extreme", "reason": "ceiling", "level": 80.0},
        {"ticker": "AVGO", "rule": "range_break", "reason": "ceiling", "level": 300.0},
    ]
    text = template.render_digest([make_suppressed()], dropped=dropped, day=date(2026, 8, 5))
    assert "2" in text
    assert "NVDA" in text and "AVGO" in text


def test_digest_omits_the_dropped_section_when_nothing_was_dropped():
    text = template.render_digest([make_suppressed()], dropped=[], day=date(2026, 8, 5))
    assert "dropped" not in text.lower()


def test_digest_on_a_quiet_day_still_says_something():
    text = template.render_digest([], dropped=[], day=date(2026, 8, 5))
    assert text.strip()
    assert "no" in text.lower()


def test_digest_never_says_buy():
    text = template.render_digest([make_suppressed()], dropped=[], day=date(2026, 8, 5))
    assert "buy" not in text.lower()


def test_digest_withholds_a_headline_carrying_a_recommendation():
    # A third-party rating headline is exactly the recommendation language the
    # no-"buy" constraint exists to keep out. It is withheld, and the fact that
    # it was withheld is stated rather than hidden.
    news = [
        NewsItem(
            ticker="GOOGL",
            headline="Analyst upgrades Alphabet to Buy",
            source="Reuters",
            url="https://example.com/a",
            published_at="2026-08-05T12:00:00Z",
        ),
        NewsItem(
            ticker="GOOGL",
            headline="Alphabet opens a new data centre",
            source="Reuters",
            url="https://example.com/b",
            published_at="2026-08-05T13:00:00Z",
        ),
    ]
    text = template.render_digest([], dropped=[], day=date(2026, 8, 5), news=news)
    assert "buy" not in text.lower()
    assert "data centre" in text
    assert "withheld" in text.lower()


def test_digest_titles_itself_by_kind():
    assert "Pre-market" in plain(
        template.render_digest([], dropped=[], day=date(2026, 8, 5), title="Pre-market digest")
    )


def test_digest_is_escaped_end_to_end():
    text = template.render_digest([make_suppressed()], dropped=[], day=date(2026, 8, 5))
    assert "2026\\-08\\-05" in text


# --------------------------------------------------------------------------
# Section shaping — defects found by the first live dry run, 2026-08-06
# --------------------------------------------------------------------------


def news_item(ticker: str, headline: str, published_at: str, url: str) -> NewsItem:
    return NewsItem(
        ticker=ticker,
        headline=headline,
        source="Reuters",
        url=url,
        published_at=published_at,
    )


def test_headline_links_stay_resolvable():
    text = template.render_digest(
        [],
        dropped=[],
        day=date(2026, 8, 5),
        news=[news_item("GOOGL", "A thing happened", "2026-08-05T12:00:00Z",
                        "https://finnhub.io/api/news?id=abc.def")],
    )
    assert "(https://finnhub.io/api/news?id=abc.def)" in text


def test_headlines_carry_their_ticker():
    text = template.render_digest(
        [],
        dropped=[],
        day=date(2026, 8, 5),
        news=[news_item("NVDA", "A thing happened", "2026-08-05T12:00:00Z", "https://x.test/a")],
    )
    assert "$NVDA" in text


def test_the_news_cap_keeps_the_freshest_not_the_first_ticker_listed():
    news = [
        news_item("GOOGL", f"Old {i}", f"2026-08-05T0{i}:00:00Z", f"https://x.test/g{i}")
        for i in range(1, 9)
    ] + [news_item("NVDA", "Fresh", "2026-08-05T23:00:00Z", "https://x.test/n")]

    text = template.render_digest([], dropped=[], day=date(2026, 8, 5), news=news)
    assert "Fresh" in text


def test_a_repeating_macro_release_appears_once_at_its_next_date():
    # FRED publishes an FOMC row for most days in a window. Unfiltered, one
    # release fills the section and pushes CPI out of the cap entirely.
    macro = [MacroEvent(date=f"2026-08-0{d}", name="FOMC Press Release") for d in range(6, 10)]
    macro.append(MacroEvent(date="2026-08-12", name="Consumer Price Index"))

    text = plain(template.render_digest([], dropped=[], day=date(2026, 8, 5), macro=macro))
    assert text.count("FOMC Press Release") == 1
    assert "2026-08-06 FOMC Press Release" in text
    assert "Consumer Price Index" in text


def test_a_headline_using_an_inflected_form_is_also_withheld():
    # "Thinking of Buying Tesla Stock on the Dip?" reached a live dry run on
    # 2026-08-06 past a \b(buy|sell)\b pattern. The constraint is that the word
    # "buy" appears in no output, and this headline carries it.
    text = template.render_digest(
        [],
        dropped=[],
        day=date(2026, 8, 5),
        news=[
            news_item("NVDA", "Thinking of Buying Tesla Stock on the Dip?",
                      "2026-08-05T12:00:00Z", "https://x.test/a"),
            news_item("NVDA", "Selling pressure waits in the wings",
                      "2026-08-05T11:00:00Z", "https://x.test/b"),
        ],
    )
    assert "buy" not in text.lower()
    assert "sell" not in text.lower()
    assert "2 headline" in plain(text)


def test_no_reserved_character_escapes_the_digest_unescaped():
    """The check that was missing on 2026-08-06.

    `test_every_special_in_an_alert_is_escaped_...` covered render_alert only,
    so a bare "- " bullet in the headline section reached a live send and
    Telegram rejected the whole digest with HTTP 400 "Character '-' is reserved".
    Every section is exercised here, and link targets are removed first because
    a URL follows different rules inside `(...)`.
    """
    import re as _re

    text = template.render_digest(
        [make_suppressed(), make_suppressed(ticker="NVDA", rule="range_break",
                                            detail="20-session high")],
        dropped=[{"ticker": "AVGO", "rule": "rsi_extreme", "reason": "ceiling", "level": 80.0}],
        day=date(2026, 8, 5),
        title="Post-close digest",
        news=[news_item("GOOGL", "A data centre opened", "2026-08-05T12:00:00Z",
                        "https://x.test/a.b?c=d")],
        earnings=[Earnings(ticker="GOOGL", date="2026-08-20", when="amc")],
        macro=[MacroEvent(date="2026-08-12", name="Consumer Price Index")],
    )

    # Link targets obey escape_md_url, not escape_md; drop them before scanning.
    scanned = _re.sub(r"\]\([^)]*\)", "]()", text)

    # `-` is never used as markup here, so every one of them must be escaped.
    assert scanned.count("-") == scanned.count(r"\-")
    # Same for `.`, which appears in every price and every date.
    assert scanned.count(".") == scanned.count(r"\.")


def test_a_missing_volume_drops_its_unit_too():
    # "Volume: n/ax 20-day average" reads as a glitch. Every intraday alert
    # takes this branch: a 15-minute window has no 20-day baseline to be a
    # multiple of. Surfaced by scripts/verify_intraday.py on 2026-08-06.
    text = plain(template.render_alert(make_suppressed(volume_ratio=None)))
    assert "Volume: n/a" in text
    assert "n/ax" not in text
    assert "20-day average" not in text


def test_a_present_volume_keeps_its_unit():
    text = plain(template.render_alert(make_suppressed(volume_ratio=1.53)))
    assert "1.5x 20-day average" in text
