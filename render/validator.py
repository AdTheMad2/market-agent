"""Reject model prose that says anything the evidence packet does not support.

This module was written before `render/gemini.py`, deliberately. A check built
after its generator gets tuned until that generator passes, and the result
tests the generator's habits rather than the claim. Everything here must hold
for any model's output, including a confidently wrong one.

**What it is not.** This is not a hallucination detector and cannot become one.
It compares tokens — numbers, tickers, dates — against a packet, so it catches
fabricated *facts* and says nothing about a sentence that is misleading using
only true numbers. That gap is covered elsewhere: the model is instructed to
add no interpretation, the template is what ships on rejection, and the word
"buy" is refused outright by both paths.

**Why rejection and not an exception.** `render/template.py` raises
`RecommendationLeak` on its own output, because that text is written in this
repository and a leak there is a bug to fix. Model output is untrusted input
arriving inside an unattended weekday job. Returning `False` routes it to the
template; raising would take the alert down with it.

See docs/IMPLEMENTATION_PLAN.md Task 5.2.
"""

from __future__ import annotations

import calendar
import re

from render.template import RECOMMENDATION_RE

# Unsigned only. A leading "-" in packet or prose is a hyphen ("150-day", an
# ISO date) far more often than a negative number, and reading it as a sign
# turns 2026-07-29 into the numbers 2026 and -7.
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Short all-caps runs. Sentence-initial words fail this: `\b[A-Z]{1,5}\b`
# cannot match "Goog" inside "Google", because the trailing boundary requires a
# non-word character after the run.
UPPER_RE = re.compile(r"\b[A-Z]{1,5}\b")

# Digit-boundary lookarounds rather than `\b`. The date this most needs to find
# is the one inside `bar_timestamp` — "2026-07-29T04:00:00Z" — where the day is
# followed by "T", a word character, so a trailing `\b` matches nothing and the
# packet ends up licensing no dates at all.
ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

_MONTHS = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): number
    for number, name in enumerate(calendar.month_abbr)
    if name
}
_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "July 29", "Jul 29, 2026", "29 July". The year is optional in both, and its
# absence means the date is checked on month and day alone.
MONTH_FIRST_RE = re.compile(
    rf"\b({_MONTH_ALTERNATION})\s+(\d{{1,2}})(?:\w*)?(?:,?\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
DAY_FIRST_RE = re.compile(
    rf"\b(\d{{1,2}})(?:\w*)?\s+({_MONTH_ALTERNATION})(?:,?\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)

# Short all-caps words this system's prose uses as words rather than as symbols.
#
# The ambiguity is genuine and unresolvable: MA is Mastercard, ET is Energy
# Transfer, AI is C3.ai. It is resolved in favour of prose because "near its
# 150-day MA" is the sentence this system exists to write, and reading MA as a
# ticker would reject every correct alert. The exposure left behind is narrow:
# a model using one of these as a company name still cannot say anything false
# about that company without a number, and numbers are checked.
DOMAIN_VOCABULARY = frozenset(
    {
        "A", "I", "AI", "AM", "PM",
        "MA", "SMA", "EMA", "RSI", "ATH", "ATL",
        "ET", "EST", "EDT", "UTC", "GMT",
        "US", "USD", "SIP", "IEX", "NYSE",
        "CEO", "CFO", "IPO", "ETF",
        "GDP", "CPI", "PPI", "FOMC", "FED", "YTD", "EPS", "Q",
    }
)


def _written_precision(token: str) -> int:
    """Decimal places as the model wrote them. `100.00` is two, `100` is none.

    The written form is what licenses a rounding: a model that writes 0.4 has
    claimed one decimal place of accuracy and may be checked at that precision,
    while one that writes 0.44 has claimed two and may not hide behind the
    first.
    """
    _, _, fraction = token.partition(".")
    return len(fraction)


def _as_number(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:  # pragma: no cover - the regex admits nothing else
        return None


def _numbers_in(text: str) -> list[tuple[float, int]]:
    found = []
    for match in NUMBER_RE.finditer(text):
        value = _as_number(match.group())
        if value is not None:
            found.append((value, _written_precision(match.group())))
    return found


def _walk(value) -> list:
    """Every scalar in the packet, at any depth."""
    if isinstance(value, dict):
        return [item for v in value.values() for item in _walk(v)]
    if isinstance(value, (list, tuple)):
        return [item for v in value for item in _walk(v)]
    return [value]


def _packet_numbers(packet: dict) -> set[float]:
    """Numeric values *and* numbers written inside packet strings.

    The strings matter as much as the floats: `detail` is "150-day", and
    without reading it the model could not name the average the trigger is
    about. `bar_timestamp` is the other one — it licenses the year, month and
    day a model may quote back.
    """
    numbers: set[float] = set()
    for item in _walk(packet):
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)):
            numbers.add(float(item))
        elif isinstance(item, str):
            numbers.update(value for value, _ in _numbers_in(item))
    return numbers


def _packet_tickers(packet: dict) -> set[str]:
    tickers: set[str] = set()
    for item in _walk(packet):
        if isinstance(item, str):
            tickers.update(UPPER_RE.findall(item))
    return tickers


def _packet_dates(packet: dict) -> set[tuple[int, int, int]]:
    dates: set[tuple[int, int, int]] = set()
    for item in _walk(packet):
        if isinstance(item, str):
            for year, month, day in ISO_DATE_RE.findall(item):
                dates.add((int(year), int(month), int(day)))
    return dates


def _licensed_number(value: float, precision: int, allowed: set[float]) -> bool:
    """True when `value` is a packet number, or a correct rounding of one.

    Exact-only would reject "100" for a level of 100.00 and every honest
    rounding besides, leaving the template as the only path that ever ships.
    Rounding widens what counts as *this* number; it never reaches a number the
    packet is not near, because the comparison is still against packet values
    one at a time.
    """
    return any(
        value == candidate or value == round(candidate, precision)
        for candidate in allowed
    )


def _dates_in(prose: str) -> list[tuple[int | None, int, int]]:
    found: list[tuple[int | None, int, int]] = []
    for year, month, day in ISO_DATE_RE.findall(prose):
        found.append((int(year), int(month), int(day)))
    for month_name, day, year in MONTH_FIRST_RE.findall(prose):
        found.append((int(year) if year else None, _MONTHS[month_name.lower()], int(day)))
    for day, month_name, year in DAY_FIRST_RE.findall(prose):
        found.append((int(year) if year else None, _MONTHS[month_name.lower()], int(day)))
    return found


def _date_is_licensed(
    named: tuple[int | None, int, int], allowed: set[tuple[int, int, int]]
) -> bool:
    year, month, day = named
    return any(
        month == m and day == d and (year is None or year == y)
        for y, m, d in allowed
    )


def validate(prose: str, packet: dict) -> bool:
    """False if any number, ticker or date in `prose` is absent from `packet`.

    The order of the checks is the order of how badly each failure would read
    in a delivered alert, not the order they are cheapest to run.
    """
    if not prose or not prose.strip():
        return False

    # A recommendation is the one failure that is wrong even when every number
    # in the sentence is right.
    if RECOMMENDATION_RE.search(prose):
        return False

    allowed_dates = _packet_dates(packet)
    for named in _dates_in(prose):
        if not _date_is_licensed(named, allowed_dates):
            return False

    # Dates are removed before the number pass so a rejected "July 30" is not
    # re-judged as the bare number 30, which some other field may license.
    numeric_prose = ISO_DATE_RE.sub(" ", prose)
    numeric_prose = MONTH_FIRST_RE.sub(" ", numeric_prose)
    numeric_prose = DAY_FIRST_RE.sub(" ", numeric_prose)

    allowed_numbers = _packet_numbers(packet)
    for value, precision in _numbers_in(numeric_prose):
        if not _licensed_number(value, precision, allowed_numbers):
            return False

    allowed_tickers = _packet_tickers(packet) | DOMAIN_VOCABULARY
    for token in UPPER_RE.findall(prose):
        if token not in allowed_tickers:
            return False

    return True
