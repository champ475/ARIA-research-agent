"""Regression tests for utils/helpers.py::extract_entity — alert-phrasing vocabulary
must never win over the actual ticker mentioned in the same message."""
from unittest.mock import patch

from utils.helpers import extract_entity, extract_all_entities, filter_valid_entities


def test_every_time_phrasing_does_not_shadow_ticker():
    assert extract_entity("alert me every time TCS drops below 3500") == "TCS"


def test_recurring_phrasing_does_not_shadow_ticker():
    assert extract_entity("set a recurring alert if RELIANCE drops below 2800") == "RELIANCE"


def test_keep_notifying_phrasing_does_not_shadow_ticker():
    assert extract_entity("keep notifying me if INFY crosses above 1500") == "INFY"


def test_plain_ticker_still_resolves():
    assert extract_entity("TCS price") == "TCS"


class TestFilterValidEntities:
    """Bug fix: extract_all_entities() accepts any capitalized 2-15 char
    token not in the (necessarily incomplete) STOP_WORDS blocklist — this
    is the gate that stops garbage tokens from reaching Planner's task
    fan-out, alert creation, and filename derivation."""

    def test_keeps_only_curated_tickers(self):
        assert filter_valid_entities(["INFY", "AN", "SET", "ALSO"]) == ["INFY"]

    def test_empty_input_returns_empty(self):
        assert filter_valid_entities([]) == []

    def test_planning_prompt_repro_yields_nothing(self):
        # "I want to retire in 20 years with a corpus of 5 crore rupees,
        # suggest a monthly SIP plan" — confirmed in production to fire 11
        # wasted NSE+BSE yfinance lookups (WANT, RETIRE, TO, IN, YEARS,
        # CORPUS, OF, CRORE, RUPEES, SIP, SUGGEST). None are real tickers,
        # so the bounded network fallback must also reject the first one.
        raw = extract_all_entities(
            "I want to retire in 20 years with a corpus of 5 crore rupees, "
            "suggest a monthly SIP plan"
        )
        assert raw, "sanity check: extract_all_entities still finds raw candidates"
        with patch("utils.helpers.ticker_has_market_data", return_value=False):
            assert filter_valid_entities(raw) == []

    def test_non_curated_but_real_ticker_survives_via_bounded_fallback(self):
        # A ticker not yet in STOCK_UNIVERSE must still resolve — this is
        # extract_all_entities()'s own documented tradeoff, not a regression
        # to fix. Only ONE network check should ever fire (bounded to the
        # first candidate), not one per raw entity.
        with patch("utils.helpers.ticker_has_market_data") as mock_check:
            mock_check.return_value = True
            result = filter_valid_entities(["NEWLISTCO"])
        assert result == ["NEWLISTCO"]
        mock_check.assert_called_once_with("NEWLISTCO")

    def test_multiple_uncurated_candidates_only_checks_the_first(self):
        with patch("utils.helpers.ticker_has_market_data", return_value=False) as mock_check:
            result = filter_valid_entities(["FOOBAR", "BAZQUX"])
        assert result == []
        mock_check.assert_called_once_with("FOOBAR")

    def test_hdfc_flexicap_no_longer_validates_bare_hdfc(self):
        # Bug fix also removed the stale bare "HDFC" entry from
        # config.py's SECTOR_UNIVERSE (HDFC Ltd merged into HDFCBANK in
        # 2023) — without that, "HDFC Flexicap" (a mutual fund name) would
        # validate "HDFC" as a real curated ticker and misroute the query.
        raw = extract_all_entities(
            "Compare HDFC Flexicap and Parag Parikh Flexicap on 5 year "
            "returns and expense ratio"
        )
        with patch("utils.helpers.ticker_has_market_data", return_value=False):
            assert filter_valid_entities(raw) == []

    def test_allow_network_fallback_false_skips_network_check(self):
        with patch("utils.helpers.ticker_has_market_data") as mock_check:
            result = filter_valid_entities(["FOOBAR"], allow_network_fallback=False)
        assert result == []
        mock_check.assert_not_called()
