"""LLM-06: casual data questions always run tools, even when the cheap
classifier calls them "simple"; definitions and chit-chat stay simple; the
simple path is told it has no live data."""

import json

import pytest

from graph.data_questions import NO_LIVE_DATA_INSTRUCTION, min_complexity
from graph.nodes.planner import build_planner_node
from graph.nodes.query_analyzer import build_query_analyzer_node

# 30 data questions phrased casually.
CASUAL_DATA = [
    "what's TCS trading at?",
    "tcs price?",
    "how much is reliance today",
    "infy pe?",
    "is HDFC Bank cheap right now",
    "how did tata steel do this week",
    "wipro share price pls",
    "what's the RSI on SBI",
    "itc dividend yield?",
    "how's maruti doing",
    "titan 52 week high?",
    "asian paints market cap",
    "is infosys overvalued",
    "what did reliance results look like",
    "how is bajaj finance performing",
    "TCS eps",
    "sun pharma outlook",
    "how much did ongc fall today",
    "kotak bank roe",
    "show me the l&t chart",
    "top gainers today",
    "how is nifty doing",
    "any bulk deals today?",
    "what's the market doing today",
    "best large cap funds by returns",
    "losers in the market now",
    "is TCS up or down today",
    "hdfcbank fundamentals",
    "NTPC technicals",
    "cipla price and pe",
]

STAYS_SIMPLE = [
    "hi",
    "thanks!",
    "what is RSI?",
    "explain P/E ratio",
    "what does NAV mean",
    "how does SIP work",
    "define CAGR",
    "what are ELSS funds",
]


class _Stub:
    def __init__(self, response):
        self.response = response

    def ask(self, prompt, **kwargs):
        return self.response


def test_set_sizes():
    assert len(CASUAL_DATA) == 30


@pytest.mark.parametrize("query", CASUAL_DATA)
def test_casual_data_question_runs_tools_even_if_classified_simple(query):
    # Worst case: the classifier says "simple" and names no companies.
    qa = build_query_analyzer_node(
        _Stub(json.dumps({"intent": "equity", "objective": query, "complexity": "simple"}))
    )
    state = {"query": query, "history": [], **qa({"query": query, "history": []})}
    assert state["complexity"] == "moderate", state["research_journal"]
    plan = build_planner_node(_Stub(json.dumps({"tools": ["fetch_top_movers"]})))(state)
    assert plan["tasks"], f"no tools ran for {query!r}"


@pytest.mark.parametrize("query", STAYS_SIMPLE)
def test_definitions_and_chitchat_stay_simple(query):
    assert min_complexity(query, [], "simple") == ("simple", None)


def test_override_never_lowers_complexity():
    assert min_complexity("hi", [], "complex") == ("complex", None)


def test_simple_path_prompt_forbids_figures():
    from graph.nodes.communication_layer import build_communication_layer_node

    out = build_communication_layer_node(_Stub("x"))(
        {"intent": "chat", "complexity": "simple", "query": "what is RSI?", "conclusions": []}
    )
    assert out["pending_synthesis_prompt"].startswith(NO_LIVE_DATA_INSTRUCTION)
