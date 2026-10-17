"""VAL-09: one Indian number/currency parser — 80 table cases plus properties."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.numbers import find_numbers, format_inr, parse_inr_amount, parse_number

D = Decimal

AMOUNTS = [
    # currency prefixes and suffixes
    ("₹12.5L", D("1250000")),
    ("₹ 3,500", D("3500")),
    ("Rs 3500", D("3500")),
    ("Rs. 3,500.75", D("3500.75")),
    ("rs.3500", D("3500")),
    ("INR 10000", D("10000")),
    ("inr 2.5 lakh", D("250000")),
    ("3500 rupees", D("3500")),
    ("3500 rs", D("3500")),
    ("₹1", D("1")),
    # Indian and Western grouping
    ("1,00,000", D("100000")),
    ("10,00,000", D("1000000")),
    ("1,00,00,000", D("10000000")),
    ("12,34,567.89", D("1234567.89")),
    ("100,000", D("100000")),
    ("1,000,000", D("1000000")),
    ("2,500", D("2500")),
    # lakh variants
    ("1.2 lakh", D("120000")),
    ("1.2L", D("120000")),
    ("1.2l", D("120000")),
    ("12 lakhs", D("1200000")),
    ("5 lac", D("500000")),
    ("5 lacs", D("500000")),
    ("5lk", D("500000")),
    ("10L", D("1000000")),
    ("0.5 lakh", D("50000")),
    # crore variants
    ("1.2 cr", D("12000000")),
    ("2cr", D("20000000")),
    ("2 Cr", D("20000000")),
    ("2 crore", D("20000000")),
    ("3 crores", D("30000000")),
    ("1.5 crs", D("15000000")),
    ("50,000 cr", D("500000000000")),
    ("2 lakh crore", D("2000000000000")),
    ("1.5 lakh crores", D("1500000000000")),
    # thousands, millions, billions
    ("10k", D("10000")),
    ("10K", D("10000")),
    ("2.5k", D("2500")),
    ("5 thousand", D("5000")),
    ("3 mn", D("3000000")),
    ("3 million", D("3000000")),
    ("1.5 bn", D("1500000000")),
    ("2 billion", D("2000000000")),
    ("4m", D("4000000")),
    # decimals, signs
    ("0.75", D("0.75")),
    (".5 lakh", D("50000")),
    ("-2500", D("-2500")),
    ("−2500", D("-2500")),
    ("+3500", D("3500")),
    ("3500.5", D("3500.5")),
    # embedded in sentences
    ("my capital is ₹12.5L", D("1250000")),
    ("I can invest 25,000 every month", D("25000")),
    ("about 1.2 cr in total", D("12000000")),
    ("alert at Rs. 2,950", D("2950")),
    ("budget: 50k", D("50000")),
    ("roughly 7.5 lakh rupees", D("750000")),
    ("₹12,50,000 only", D("1250000")),
    ("savings of 3 crore", D("30000000")),
    ("set it to 15000", D("15000")),
    ("put in 1 lakh", D("100000")),
]

PERCENTS = [
    ("15%", D("15")),
    ("15 %", D("15")),
    ("12.5 percent", D("12.5")),
    ("8 per cent", D("8")),
    ("-5%", D("-5")),
    ("ROE above 15%", D("15")),
    ("returns of 9.5pc", D("9.5")),
    ("drops 3%", D("3")),
]

NOT_ONE_AMOUNT = [
    "lots",
    "all of it",
    "",
    "-",
    "I have 10L, maybe 12 lakh",  # ambiguous: two amounts
    "between 10 and 20",
    "no idea",
    "₹",
    "5 lots of shares and 3 more",
    "a few crores",
    "15%",  # a percent is not a rupee amount
    "1.2 and 3.4",
]


@pytest.mark.parametrize(("text", "expected"), AMOUNTS)
def test_amounts(text, expected):
    p = parse_inr_amount(text)
    assert p is not None, text
    assert p.value == expected


@pytest.mark.parametrize(("text", "expected"), PERCENTS)
def test_percents(text, expected):
    p = parse_number(text)
    assert p is not None and p.percent and p.value == expected


@pytest.mark.parametrize("text", NOT_ONE_AMOUNT)
def test_not_exactly_one_amount_is_none(text):
    assert parse_inr_amount(text) is None


def test_case_count_is_at_least_80():
    assert len(AMOUNTS) + len(PERCENTS) + len(NOT_ONE_AMOUNT) >= 80


def test_span_points_at_the_number_that_was_read():
    text = "alert me if TCS drops below Rs. 3,500 in 2 days"
    first, second = find_numbers(text)
    assert text[first.span[0] : first.span[1]] == "Rs. 3,500" and first.currency
    assert second.value == 2


def test_lots_is_not_lakh():
    assert [p.value for p in find_numbers("5 lots")] == [D(5)]


def test_values_are_exact_decimals():
    assert parse_inr_amount("12345678.9012").value == D("12345678.9012")


@settings(max_examples=500, deadline=None)
@given(st.decimals(min_value=-(10**12), max_value=10**12, places=2, allow_nan=False, allow_infinity=False))
def test_format_inr_round_trips(value):
    assert parse_inr_amount(format_inr(value)).value == value


@settings(max_examples=500, deadline=None)
@given(st.text())
def test_never_raises_on_arbitrary_text(text):
    find_numbers(text)
    parse_number(text)
    parse_inr_amount(text)
