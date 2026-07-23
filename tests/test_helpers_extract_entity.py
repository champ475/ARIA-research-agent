"""Regression tests for utils/helpers.py::extract_entity — alert-phrasing vocabulary
must never win over the actual ticker mentioned in the same message."""
from utils.helpers import extract_entity


def test_every_time_phrasing_does_not_shadow_ticker():
    assert extract_entity("alert me every time TCS drops below 3500") == "TCS"


def test_recurring_phrasing_does_not_shadow_ticker():
    assert extract_entity("set a recurring alert if RELIANCE drops below 2800") == "RELIANCE"


def test_keep_notifying_phrasing_does_not_shadow_ticker():
    assert extract_entity("keep notifying me if INFY crosses above 1500") == "INFY"


def test_plain_ticker_still_resolves():
    assert extract_entity("TCS price") == "TCS"
