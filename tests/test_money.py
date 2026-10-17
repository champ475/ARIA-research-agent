"""DATA-09: float/str -> Decimal conversion at the database boundary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from utils.money import tidy, to_db, to_decimal


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.1, Decimal("0.1")),  # shortest repr, not 0.1000000000000000055...
        ("1,23,45,678.90", Decimal("12345678.90")),
        (12345678.9012, Decimal("12345678.9012")),
        (5, Decimal(5)),
        (None, None),
        ("", None),
        ("abc", None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
    ],
)
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == expected


def test_to_db_rounds_to_four_places():
    assert to_db(1.23456789) == Decimal("1.2346")
    assert to_db(None) is None
    assert str(to_db(70)) == "70.0000"


def test_tidy_strips_trailing_zeros_but_not_integers():
    assert tidy(Decimal("70.0000")) == Decimal(70) and str(tidy(Decimal("70.0000"))) == "70"
    assert str(tidy(Decimal("12.5000"))) == "12.5"
    assert str(tidy(Decimal("100.0000"))) == "100"
    assert tidy(None) is None
