"""Regression tests for utils/helpers.py::parse_screener_name. Extracted in
Phase 7.3 of the graph-expansion plan from agents/screener_agent.py's
original inline regex, which greedily captured filler words ("this screener
as") instead of the actual name when the message wasn't quoted — fixed here
so both the legacy agent and the graph path save under the same, correct
name."""
from utils.helpers import parse_screener_name


def test_quoted_name_extracted_even_with_filler_words_before_it():
    assert parse_screener_name('save this screener as "My Value Picks"') == "My Value Picks"


def test_unquoted_name_after_as_extracted():
    assert parse_screener_name("save it as ValuePicks") == "ValuePicks"


def test_no_recognizable_pattern_defaults_to_generic_name():
    assert parse_screener_name("save it") == "My Screener"


def test_single_quoted_name_extracted():
    assert parse_screener_name("save as 'Quality Screen'") == "Quality Screen"
