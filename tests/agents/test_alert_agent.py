"""Tests for agents/alert_agent.py — regex extraction and recurring-alert detection."""
from unittest.mock import MagicMock, patch

import pytest

from agents.alert_agent import AlertAgent


@pytest.fixture
def agent():
    with patch("agents.alert_agent.LLMClient"):
        return AlertAgent(user_id=1)


class TestRegexExtract:
    def test_price_below(self, agent):
        atype, threshold, condition = agent._regex_extract("alert me if TCS drops below 3500", "TCS")
        assert atype == "price_below"
        assert threshold == 3500.0

    def test_price_above(self, agent):
        atype, threshold, condition = agent._regex_extract("notify me if RELIANCE goes above 2900", "RELIANCE")
        assert atype == "price_above"
        assert threshold == 2900.0

    def test_indicator_keyword_forces_llm_path(self, agent):
        atype, threshold, condition = agent._regex_extract("notify me when RELIANCE RSI crosses below 30", "RELIANCE")
        assert atype is None  # bails out to LLM extraction, per _INDICATOR_HINT_KW


class TestRecurringDetection:
    def test_create_alert_one_shot_by_default(self, agent):
        with patch("agents.alert_agent.alert_add", return_value=42) as mock_add:
            resp = agent._create_alert("alert me if TCS drops below 3500")
        assert mock_add.call_args.kwargs["recurring"] is False
        assert "fires once" in resp

    def test_create_alert_recurring_when_phrased(self, agent):
        with patch("agents.alert_agent.alert_add", return_value=43) as mock_add:
            resp = agent._create_alert("alert me every time TCS drops below 3500")
        _, kwargs = mock_add.call_args
        assert kwargs["recurring"] is True
        assert "recurring" in resp.lower()
