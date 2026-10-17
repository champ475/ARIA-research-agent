"""LLM-10: 25 multi-turn cases for entity carry-over. A follow-up with no
company name inherits the one company of the last company-bearing user turn;
after a multi-company turn it asks which one. No routing badge reaches the
QueryAnalyzer prompt."""

from __future__ import annotations

import json

import pytest

from graph.entity_resolution import blocking_resolutions, clarification_text, history_carry
from graph.nodes.query_analyzer import build_query_analyzer_node

U, A = "user", "assistant"


def h(*turns: tuple[str, str]) -> list[dict]:
    return [{"role": r, "content": c} for r, c in turns]


# (history, follow-up, expected entities, ambiguous candidates)
CASES = [
    (h((U, "How is TCS doing?"), (A, "TCS is up.")), "what about its RSI", ["TCS"], []),
    (h((U, "analyse Reliance fundamentals"), (A, "ok")), "and the news?", ["RELIANCE"], []),
    (h((U, "what's the PE of Infosys"), (A, "x")), "show me the chart", ["INFY"], []),
    (h((U, "HDFC Bank technical analysis"), (A, "x")), "any news on it", ["HDFCBANK"], []),
    (h((U, "RSI for SBI"), (A, "x")), "and MACD?", ["SBIN"], []),
    (h((U, "compare TCS and Infosys"), (A, "x")), "what about its RSI", [], ["TCS", "INFY"]),
    (h((U, "compare TCS and Infosys"), (A, "x")), "which has the better ROE", [], ["TCS", "INFY"]),
    (
        h((U, "ICICI Bank vs Axis Bank fundamentals"), (A, "x")),
        "show its chart",
        [],
        ["ICICIBANK", "AXISBANK"],
    ),
    (h((U, "Cipla vs Dr Reddys"), (A, "x")), "and the news", [], ["CIPLA", "DRREDDY"]),
    (h((U, "TCS vs Infosys vs Wipro"), (A, "x")), "its dividend yield", [], ["TCS", "INFY", "WIPRO"]),
    # a later single-company turn settles the reference again
    (
        h((U, "compare TCS and Infosys"), (A, "x"), (U, "tell me more about Infosys"), (A, "x")),
        "its RSI",
        ["INFY"],
        [],
    ),
    (h((U, "How is TCS doing?"), (A, "x"), (U, "and Wipro?"), (A, "x")), "its RSI", ["WIPRO"], []),
    (h((U, "Titan valuation"), (A, "x"), (U, "thanks"), (A, "welcome")), "and its debt", ["TITAN"], []),
    (h((U, "hello"), (A, "hi"), (U, "ITC news"), (A, "x")), "and the price", ["ITC"], []),
    (h((U, "Maruti Suzuki 52 week high"), (A, "x")), "what about the low", ["MARUTI"], []),
    (h((U, "Coal India market cap"), (A, "x")), "dividend?", ["COALINDIA"], []),
    (h((U, "NTPC bollinger bands"), (A, "x")), "volume?", ["NTPC"], []),
    (h((U, "Bajaj Finance dividend yield"), (A, "x")), "and its ROE", ["BAJFINANCE"], []),
    (
        h((U, "Hindalco earnings trend"), (A, "x"), (U, "Tata Steel momentum"), (A, "x")),
        "its beta",
        ["TATASTEEL"],
        [],
    ),
    (h((U, "Nestle India margins"), (A, "x")), "compare with its history", ["NESTLEIND"], []),
    # nothing to carry
    (h(), "what about its RSI", [], []),
    (h((U, "what is a mutual fund"), (A, "x")), "what about its RSI", [], []),
    (h((U, "hello"), (A, "hi")), "its price?", [], []),
    # assistant turns never supply the entity
    (h((A, "TCS and Infosys are both IT stocks")), "its RSI", [], []),
    (h((U, "ONGC recent news and price"), (A, "Reliance and BPCL also moved")), "its RSI", ["ONGC"], []),
]


def test_there_are_25_cases():
    assert len(CASES) == 25


@pytest.mark.parametrize("history,query,entities,ambiguous", CASES)
def test_carry_over_or_clarify(history, query, entities, ambiguous):
    got, amb = history_carry(history)
    assert got == entities
    assert sorted(amb) == sorted(ambiguous)


@pytest.mark.parametrize("history,query,entities,ambiguous", [c for c in CASES if c[3]])
def test_analyzer_asks_which_company(history, query, entities, ambiguous):
    class Stub:
        def ask(self, prompt, **kw):
            return json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"})

    state = {"query": query, "history": history}
    out = build_query_analyzer_node(Stub())(state)
    assert out["entities"] == []
    blocking = blocking_resolutions({**state, **out})
    assert blocking, "an ambiguous reference must stop research"
    text = clarification_text(blocking)
    assert all(sym in text for sym in ambiguous)


def test_badge_never_reaches_the_analyzer_prompt():
    seen = []

    class Stub:
        def ask(self, prompt, **kw):
            seen.append(prompt)
            return json.dumps({"intent": "equity", "objective": "x", "complexity": "simple"})

    history = h((U, "How is TCS doing?"), (A, "*🔀 Routed to: Equity*\n\nTCS is up."))
    build_query_analyzer_node(Stub())({"query": "its RSI", "history": history})
    assert "Routed to" not in seen[0] and "TCS is up." in seen[0]
