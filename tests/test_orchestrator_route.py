"""
Characterization tests for agents/orchestrator.py::route() (lines 73-95).

This function had zero test coverage before Phase 7.0 of the graph-expansion
plan (see ARCHITECTURE.md / RFC_AGENTIC_RESEARCH_GRAPH.md). These tests pin
down CURRENT behavior as a regression net for the staged retirement in
Phase 7.4 — they are not a claim that the keyword-scoring logic is correct or
should stay, only that later phases must not silently change it without
noticing.
"""
from unittest.mock import patch

from agents.orchestrator import route


def test_report_keyword_routes_to_report():
    assert route("please write a full report on TCS") == "report"


def test_screener_keyword_routes_to_screener():
    assert route("show me top gainers today") == "screener"


def test_mf_keyword_routes_to_mf():
    assert route("what's the expense ratio of this fund") == "mf"


def test_alert_keyword_routes_to_alert():
    assert route("alert me if TCS drops below 3000") == "alert"


def test_alert_crud_regex_routes_to_alert():
    # Bare "cancel alert" phrase list misses this shape; the CRUD regex
    # (_ALERT_CRUD_RE) is what's expected to catch it.
    assert route("cancel my TCS alert please") == "alert"


def test_planning_keyword_routes_to_planning():
    assert route("help me build a financial plan for retirement") == "planning"


def test_no_keyword_match_defaults_to_equity():
    assert route("what do you think about TCS") == "equity"


def test_empty_message_defaults_to_equity():
    assert route("") == "equity"


def test_tie_break_prefers_report_over_screener_by_dict_order():
    # "screen" is a screener keyword; "generate report" is a report keyword.
    # max() on a dict with equal scores returns the first key in insertion
    # order, and "report" is inserted before "screener" in the scores dict
    # (agents/orchestrator.py:77-82) -- pin this down explicitly since it's
    # an implicit tie-break that later phases might change unintentionally.
    msg = "generate report and screen for value stocks"
    assert route(msg) == "report"


def test_planning_continuation_without_ticker_stays_on_planning():
    with patch("agents.orchestrator.validate_ticker", return_value=False):
        assert route("I'm 30 years old with moderate risk tolerance", last_tab="planning") == "planning"


def test_planning_continuation_with_valid_ticker_switches_to_equity():
    with patch("agents.orchestrator.validate_ticker", return_value=True):
        assert route("actually tell me about TCS instead", last_tab="planning") == "equity"


def test_last_tab_equity_has_no_planning_stickiness():
    # Without last_tab="planning", an ambiguous message with no keyword hits
    # always falls through to the equity catch-all regardless of ticker validity.
    with patch("agents.orchestrator.validate_ticker", return_value=False):
        assert route("what should I do next", last_tab="equity") == "equity"
