"""VAL-04: alert specs. tests/data/alert_cases.csv holds 60 phrasings across
all 14 alert types, Indian number formats, both directions and bad ranges;
every one must yield exactly the expected AlertSpec or a refusal."""

import csv
from pathlib import Path

import pytest

from graph.alert_spec import (
    ALERT_TYPES,
    AlertRefusal,
    AlertSpec,
    alert_id_in,
    already_true,
    confirms_anyway,
    parse_alert,
    spec_from_llm,
)
from utils.indian_numbers import format_inr, parse_amount

with open(Path(__file__).parent / "data" / "alert_cases.csv", encoding="utf-8") as _f:
    CASES = list(csv.DictReader(_f))


def test_table_covers_60_phrasings_and_all_14_types():
    assert len(CASES) == 60
    assert set(ALERT_TYPES) <= {c["expect"] for c in CASES}
    assert sum(c["expect"] == "refuse" for c in CASES) >= 10


@pytest.mark.parametrize("case", CASES, ids=[c["message"] for c in CASES])
def test_phrasing_gives_exact_spec_or_refusal(case):
    result = parse_alert(case["message"], "TCS")
    if case["expect"] == "refuse":
        assert isinstance(result, AlertRefusal), result
        assert result.reason
        return
    assert isinstance(result, AlertSpec), result
    assert result.alert_type == case["expect"]
    expected_threshold = float(case["threshold"]) if case["threshold"] else None
    assert (
        result.threshold == pytest.approx(expected_threshold)
        if expected_threshold
        else result.threshold is None
    )
    assert (result.direction or "") == case["direction"]


def test_the_comparator_number_wins_not_the_last_number():
    spec = parse_alert("alert if TCS falls below 3500 in the next 2 weeks of 2026", "TCS")
    assert spec.alert_type == "price_below" and spec.threshold == 3500


def test_unrecognised_phrasing_returns_none_for_the_llm_fallback():
    assert parse_alert("set an alert for TCS", "TCS") is None


def test_llm_output_is_validated_like_any_other_spec():
    assert isinstance(
        spec_from_llm({"alert_type": "rsi_cross_below", "threshold": 130}, "TCS", ""), AlertRefusal
    )
    ok = spec_from_llm({"alert_type": "pct_change", "threshold": "4", "direction": "down"}, "TCS", "")
    assert isinstance(ok, AlertSpec) and ok.direction == "down"
    assert spec_from_llm({"alert_type": "teleport"}, "TCS", "") is None


def test_describe_is_plain_words():
    assert (
        parse_alert("alert if TCS goes above 1.2 lakh", "TCS").describe()
        == "TCS: price goes above ₹1,20,000.00"
    )
    assert parse_alert("recurring alert if TCS drops 5%", "TCS").describe() == (
        "TCS: price falls 5% or more in a day (recurring)"
    )


def test_already_true_and_confirmation_and_ids():
    below = parse_alert("alert if TCS falls below 3500", "TCS")
    assert already_true(below, 3400.0) and not already_true(below, 3600.0) and not already_true(below, None)
    assert confirms_anyway("alert if TCS falls below 3500 anyway")
    assert not confirms_anyway("alert if TCS falls below 3500")
    assert alert_id_in("cancel alert #12") == 12
    assert alert_id_in("pause alert 7 please") == 7
    assert alert_id_in("cancel my TCS alert") is None


@pytest.mark.parametrize(
    "text,value",
    [
        ("₹3,500", 3500),
        ("Rs. 3500", 3500),
        ("3.5k", 3500),
        ("1.2 lakh", 120000),
        ("1.2L", 120000),
        ("12 lakhs", 1_200_000),
        ("2 crore", 20_000_000),
        ("2cr", 20_000_000),
        ("1,20,000", 120000),
        (5000, 5000),
    ],
)
def test_indian_amounts(text, value):
    assert parse_amount(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["lots", "all of it", "", "3500 or 4000", None])
def test_unclear_amounts_are_none(text):
    assert parse_amount(text) is None


def test_format_inr_uses_indian_grouping():
    assert format_inr(1234567.5) == "₹12,34,567.50"
    assert format_inr(999) == "₹999.00"
