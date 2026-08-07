"""The evidence packet is the model's entire world.

Every test here exists to hold one line: nothing reaches the model that is not
in this packet, and nothing is in this packet that was not computed from the
inputs. The validator in Task 5.2 checks the model's output against exactly
this dict, so a number missing here is a number the model may not say — and a
key added here that nobody computed is a number the model may fabricate freely.
"""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from engine.suppressors import Suppressed
from engine.triggers import Trigger
from render.evidence import PACKET_KEYS, PRECISION, build_packet
from sources import NewsItem


@pytest.fixture
def trigger() -> Trigger:
    return Trigger(
        ticker="GOOGL",
        rule="ma_proximity",
        level=336.57,
        price=335.76,
        distance_pct=0.24,
        volume_ratio=1.8,
        rsi=61.2,
        bar_timestamp="2026-07-29T04:00:00Z",
        watchlist="core",
        detail="150-day",
    )


@pytest.fixture
def suppressed(trigger: Trigger) -> Suppressed:
    return Suppressed(trigger=trigger, demoted=False, reason=None)


def test_every_numeric_field_on_the_trigger_reaches_the_packet(suppressed):
    """The plan's wording, tested literally.

    Not a spot-check of the fields that happen to matter today: the assertion
    walks `Trigger`'s own fields, so a numeric field added to the trigger in a
    later phase fails this test until the packet carries it. The alternative is
    a field the model can never mention because the validator has never heard
    of it.
    """
    packet = build_packet(suppressed, [])
    numeric = [
        f.name
        for f in fields(Trigger)
        if f.type in ("float", "float | None", "int")
    ]
    assert numeric, "the fixture stopped finding numeric fields; check the types"
    for name in numeric:
        assert name in packet, f"{name} is on Trigger and missing from the packet"
        expected = getattr(suppressed.trigger, name)
        if name in PRECISION and expected is not None:
            expected = round(expected, PRECISION[name])
        assert packet[name] == expected


def test_numbers_arrive_at_the_precision_the_reader_will_see(trigger):
    """A float is not a displayed number, and the model is told to copy.

    A live pipeline run produced "crossed its 50-day moving average level of
    356.50559999999996" beside a table reading 356.51. The model had copied the
    packet exactly, as instructed, and the validator accepted an exact match, as
    designed. The packet was the thing that was wrong.
    """
    raw = Suppressed(
        trigger=Trigger(
            **{
                **trigger.__dict__,
                "level": 356.50559999999996,
                "price": 354.29999999999995,
                "distance_pct": 0.6199999999999999,
                "volume_ratio": 0.6499999999999999,
                "rsi": 50.74999999999999,
            }
        ),
        demoted=False,
        reason=None,
    )
    packet = build_packet(raw, [])
    assert packet["level"] == 356.51
    assert packet["price"] == 354.3
    assert packet["distance_pct"] == 0.62
    assert packet["volume_ratio"] == 0.6
    assert packet["rsi"] == 50.7


def test_the_unrounded_float_is_no_longer_something_the_model_may_say(trigger):
    """The rounding is not cosmetic — it narrows what the validator licenses."""
    from render.validator import validate

    raw = Suppressed(
        trigger=Trigger(**{**trigger.__dict__, "level": 356.50559999999996}),
        demoted=False,
        reason=None,
    )
    packet = build_packet(raw, [])
    assert validate("GOOGL crossed its 150-day average of 356.51", packet) is True
    assert (
        validate("GOOGL crossed its 150-day average of 356.50559999999996", packet)
        is False
    )


def test_the_packet_carries_no_key_nobody_computed(suppressed):
    packet = build_packet(suppressed, [])
    assert set(packet) == set(PACKET_KEYS)


def test_the_packet_is_json_safe(suppressed):
    """It is serialised into a prompt. A dataclass or a date that survives
    locally and raises inside the Gemini client fails at the worst moment."""
    packet = build_packet(suppressed, [])
    assert json.loads(json.dumps(packet)) == packet


def test_a_none_valued_number_stays_none_rather_than_vanishing(trigger):
    """`rsi` is None for every rule that is not an RSI rule.

    Dropping the key instead would make the packet's shape depend on the rule,
    and the validator flattens whatever it is given — a missing key and a null
    key are the same to it, but they are not the same to a prompt that lists
    the fields the model may use.
    """
    without_rsi = Suppressed(
        trigger=Trigger(**{**trigger.__dict__, "rsi": None, "volume_ratio": None}),
        demoted=False,
        reason=None,
    )
    packet = build_packet(without_rsi, [])
    assert packet["rsi"] is None
    assert packet["volume_ratio"] is None


def test_the_suppression_shows_in_the_packet(trigger):
    demoted = Suppressed(
        trigger=trigger, demoted=True, reason="earnings within 3 days"
    )
    packet = build_packet(demoted, [])
    assert packet["demoted"] is True
    assert packet["suppression_reason"] == "earnings within 3 days"


def test_headlines_arrive_whole_with_their_source_and_time(suppressed):
    news = [
        NewsItem(
            ticker="GOOGL",
            headline="Alphabet opens a data centre in Ohio",
            source="Reuters",
            url="https://example.com/a",
            published_at="2026-07-29T12:00:00Z",
        )
    ]
    packet = build_packet(suppressed, news)
    assert packet["news"] == [
        {
            "ticker": "GOOGL",
            "headline": "Alphabet opens a data centre in Ohio",
            "source": "Reuters",
            "url": "https://example.com/a",
            "published_at": "2026-07-29T12:00:00Z",
        }
    ]


def test_a_headline_carrying_a_recommendation_word_never_reaches_the_model(
    suppressed,
):
    """The same filter the template applies, applied one step earlier.

    `render/template.py` withholds these from the digest. If the packet still
    carried them, the model would read "upgraded to Buy" and could echo it in
    prose that the template's assertion never sees, because that assertion
    guards template output and not the model's.
    """
    news = [
        NewsItem(
            ticker="GOOGL",
            headline="Analyst upgrades Alphabet to Buy",
            source="Reuters",
            url="https://example.com/b",
            published_at="2026-07-29T12:00:00Z",
        ),
        NewsItem(
            ticker="GOOGL",
            headline="Alphabet opens a data centre in Ohio",
            source="Reuters",
            url="https://example.com/a",
            published_at="2026-07-29T12:00:00Z",
        ),
    ]
    packet = build_packet(suppressed, news)

    assert [item["headline"] for item in packet["news"]] == [
        "Alphabet opens a data centre in Ohio"
    ]
    assert packet["headlines_withheld"] == 1


def test_headlines_for_other_tickers_are_not_in_this_packet(suppressed):
    """One packet describes one trigger. A NVDA headline in a GOOGL packet
    licenses the validator to accept "NVDA" in GOOGL prose."""
    news = [
        NewsItem(
            ticker="NVDA",
            headline="Nvidia opens a data centre in Ohio",
            source="Reuters",
            url="https://example.com/c",
            published_at="2026-07-29T12:00:00Z",
        )
    ]
    assert build_packet(suppressed, news)["news"] == []
