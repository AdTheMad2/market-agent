"""The adversarial half of Phase 5.

This file is written before `render/gemini.py` exists, on purpose. Building the
generator first creates pressure to loosen the check until the generator's
output happens to pass, and a check tuned to its generator's habits is not a
check. Every test below states something that must be true of *any* model's
output, including a model that is confidently wrong.

The failure this guards against is the one the whole project is shaped around:
a plausible-looking alert nobody questions. A fabricated price is not a typo —
it is the system stating, in its own voice, that a level was touched when it
was not.
"""

from __future__ import annotations

import pytest

from render.validator import validate


@pytest.fixture
def packet() -> dict:
    """A GOOG proximity trigger. Deliberately round numbers: the point of each
    test is which tokens are licensed, not the arithmetic behind them."""
    return {
        "ticker": "GOOG",
        "rule": "ma_proximity",
        "detail": "150-day",
        "level": 100.00,
        "price": 100.40,
        "distance_pct": 0.4,
        "volume_ratio": None,
        "rsi": None,
        "bar_timestamp": "2026-07-29T04:00:00Z",
        "watchlist": "core",
        "demoted": False,
        "suppression_reason": None,
        "news": [],
        "headlines_withheld": 0,
    }


# --------------------------------------------------------------------------
# The three the plan names
# --------------------------------------------------------------------------


def test_rejects_fabricated_number(packet):
    assert validate("GOOG is 3% above its 150-day MA of 412.50", packet) is False


def test_accepts_prose_using_only_packet_values(packet):
    assert validate("GOOG is 0.4% from its 150-day MA of 100.00", packet) is True


def test_rejects_fabricated_ticker(packet):
    assert validate("GOOG and MSFT both sit near support", packet) is False


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def test_formatting_is_not_fabrication(packet):
    """100.00, 100.0 and 100 are one number written three ways. A validator
    that rejected these would reject all correct prose and the fallback would
    be the only path that ever ran."""
    for written in ("100.00", "100.0", "100"):
        assert validate(f"GOOG sits at its 150-day MA of {written}", packet) is True


def test_a_correct_rounding_is_accepted(packet):
    packet["distance_pct"] = 0.44
    assert validate("GOOG is 0.4% from its 150-day MA of 100.00", packet) is True


def test_a_rounding_that_is_not_one_is_rejected(packet):
    """0.5 is not a rounding of 0.44, and the difference between "within half a
    percent" and "within four tenths" is the difference between a level that is
    close and one that is closer."""
    packet["distance_pct"] = 0.44
    assert validate("GOOG is 0.5% from its 150-day MA of 100.00", packet) is False


def test_rounding_is_not_a_licence_to_invent(packet):
    """Rounding tolerance widens what counts as *this* number. It must not
    widen to a number nothing in the packet is near."""
    assert validate("GOOG is 0.4% from its 150-day MA of 412", packet) is False


def test_a_thousands_separator_is_the_same_number(packet):
    packet["level"] = 1234.5
    packet["price"] = 1234.5
    assert validate("GOOG sits at its 150-day MA of 1,234.5", packet) is True


def test_numbers_inside_packet_strings_are_licensed(packet):
    """`detail` is "150-day". Without reading numbers out of packet *strings*,
    the model could not name the average the trigger is about."""
    assert validate("GOOG is near the 150-day average", packet) is True


def test_a_number_from_a_field_that_is_none_is_not_licensed(packet):
    """`rsi` is None for a proximity trigger. A model that mentions an RSI has
    invented one, and "RSI 61" reads exactly as authoritative as a real one."""
    assert validate("GOOG is near its 150-day MA, RSI 61", packet) is False


# --------------------------------------------------------------------------
# Tickers
# --------------------------------------------------------------------------


def test_domain_vocabulary_is_not_read_as_a_ticker(packet):
    """MA, RSI and ET are uppercase and short. They are also, respectively,
    Mastercard, an indicator and Energy Transfer. The ambiguity is real and
    resolved in favour of prose: this system's output uses these as words, and
    treating them as tickers would reject every correct sentence. The exposure
    is that a model naming Mastercard as "MA" passes — it would still have to
    fabricate a number to say anything false about it."""
    assert validate("GOOG is near its 150-day MA", packet) is True


def test_a_ticker_from_a_licensed_headline_is_accepted(packet):
    packet["news"] = [
        {
            "ticker": "GOOG",
            "headline": "GOOG opens a data centre in Ohio",
            "source": "Reuters",
            "url": "https://example.com/a",
            "published_at": "2026-07-29T12:00:00Z",
        }
    ]
    assert validate("GOOG is near its 150-day MA after opening a data centre", packet) is True


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_the_bar_date_may_be_named_in_words(packet):
    assert validate("GOOG closed near its 150-day MA on July 29", packet) is True


def test_a_date_the_packet_does_not_contain_is_rejected(packet):
    """The timestamp is the one claim this system makes about freshness
    (docs/SPEC.md §4.3). A model shifting it by a day undoes that."""
    assert validate("GOOG closed near its 150-day MA on July 30", packet) is False


def test_the_iso_date_is_accepted(packet):
    assert validate("GOOG, bar 2026-07-29, near its 150-day MA", packet) is True


# --------------------------------------------------------------------------
# The constraints that are not about arithmetic
# --------------------------------------------------------------------------


def test_a_recommendation_word_is_rejected_not_raised(packet):
    """`render/template.py` raises `RecommendationLeak` on its own output,
    because that text is written in this repository and a leak is a bug. Model
    output is different: it is untrusted input, arriving at 14:54 on a weekday
    inside a job nobody is watching. Rejection routes it to the template.
    Raising would take the alert down with it."""
    assert validate("GOOG is near its 150-day MA — a buy signal", packet) is False


def test_empty_prose_is_not_valid(packet):
    for prose in ("", "   ", "\n"):
        assert validate(prose, packet) is False


def test_prose_with_no_numbers_at_all_is_valid(packet):
    """Nothing requires the model to quote a number. It is required not to
    invent one."""
    assert validate("GOOG is sitting close to a long-term average.", packet) is True
