"""
Phase 7.4: Orchestrator.classify() — the method app.py:102 calls to decide
whether to append a timeframe keyword, sourced from the same route() call
process_stream() uses internally, so the pre-check and the real routing
decision can never disagree.
"""
from unittest.mock import patch

from agents.orchestrator import Orchestrator


def _orchestrator_without_llm_init():
    """Orchestrator.__init__ constructs all 6 specialist agents (each with
    its own LLMClient) — patch LLMClient construction out entirely so this
    test doesn't depend on real API keys being configured."""
    with patch("agents.llm_client.LLMClient._init", return_value=None):
        return Orchestrator(user_id=1)


def test_classify_delegates_to_module_level_route():
    orch = _orchestrator_without_llm_init()
    with patch("agents.orchestrator.route", return_value="screener") as mock_route:
        result = orch.classify("top gainers today")
    mock_route.assert_called_once_with("top gainers today", last_tab=orch._last_tab)
    assert result == "screener"


def test_classify_reflects_last_tab_continuity():
    orch = _orchestrator_without_llm_init()
    orch._last_tab = "planning"
    with patch("agents.orchestrator.validate_ticker", return_value=False):
        assert orch.classify("I'm 30 years old with moderate risk") == "planning"
