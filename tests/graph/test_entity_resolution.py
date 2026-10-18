"""VAL-02 at the query level: which companies a message is about, when the
conversation history may supply one, and how unresolved names stop a turn."""

import json

from graph.entity_resolution import (
    blocking_resolutions,
    clarification_text,
    entity_notes_text,
    extract_mentions,
    resolve_mentions,
)
from graph.nodes.communication_layer import build_communication_layer_node
from graph.nodes.planner import build_planner_node
from graph.nodes.query_analyzer import build_query_analyzer_node


class _Stub:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def ask(self, prompt, **kwargs):
        self.calls += 1
        return self.response


def _qa(intent="equity", companies=None, complexity="moderate"):
    return build_query_analyzer_node(
        _Stub(
            json.dumps(
                {"intent": intent, "objective": "x", "complexity": complexity, "companies": companies or []}
            )
        )
    )


TCS_HISTORY = [
    {"role": "user", "content": "analyse TCS"},
    {"role": "assistant", "content": "TCS looks fine."},
]


def test_unknown_ticker_after_tcs_is_not_found_not_tcs():
    out = _qa()({"query": "what about XYZQ price", "history": TCS_HISTORY})
    assert out["entities"] == []
    assert [(u["query"], u["status"]) for u in out["entity_resolutions"]] == [("XYZQ", "not_found")]


def test_history_supplies_the_entity_only_when_no_company_is_named():
    out = _qa()({"query": "what about its RSI?", "history": TCS_HISTORY})
    assert out["entities"] == ["TCS"] and out["entity_resolutions"] == []


def test_llm_mentions_resolve_typos_but_cannot_invent_tickers():
    out = _qa(companies=["relaince industries", "MadeUpCo"])({"query": "how is relaince industries doing"})
    assert out["entities"] == ["RELIANCE"]
    assert [u["query"] for u in out["entity_resolutions"]] == ["MadeUpCo"]


def test_fund_names_and_jargon_are_not_company_mentions():
    for q in (
        "HDFC Flexi Cap Fund NAV",
        "best ELSS funds for 3 years",
        "screen stocks with PE under 20 and ROE above 15",
    ):
        assert extract_mentions(q) == [], q
    assert resolve_mentions("SBI Small Cap Fund vs HDFC Bank").entities == ["HDFCBANK"]


def test_shouting_does_not_turn_every_word_into_a_ticker():
    assert resolve_mentions("WHAT IS THE LATEST PRICE OF TCS").entities == ["TCS"]


def test_ambiguous_name_blocks_research_and_gets_a_question():
    state = {
        "query": "price of HDFC",
        "intent": "equity",
        "complexity": "moderate",
        **{k: v for k, v in _qa()({"query": "price of HDFC"}).items() if k.startswith("entit")},
    }
    planner_client = _Stub("{}")
    plan = build_planner_node(planner_client)(state)
    assert plan["tasks"] == [] and planner_client.calls == 0
    comm_client = _Stub("should not be called")
    out = build_communication_layer_node(comm_client)({**state, "conclusions": []})
    assert comm_client.calls == 0
    assert "Which **HDFC** do you mean?" in out["final_report"]
    assert "HDFCBANK" in out["final_report"] and out["pending_synthesis_prompt"] is None


def test_not_found_blocks_only_company_intents_and_only_when_nothing_resolved():
    nf = [{"query": "XYZQ", "status": "not_found", "candidates": [], "note": None}]
    assert blocking_resolutions({"intent": "equity", "entities": [], "entity_resolutions": nf}) == nf
    assert blocking_resolutions({"intent": "equity", "entities": ["TCS"], "entity_resolutions": nf}) == []
    assert blocking_resolutions({"intent": "mf", "entities": [], "entity_resolutions": nf}) == []


def test_clarification_and_notes_text():
    text = clarification_text(
        [
            {
                "query": "XYZQ",
                "status": "not_found",
                "candidates": [{"symbol": "XYZ", "name": "Xyz Ltd"}],
                "note": None,
            }
        ]
    )
    assert "couldn't find a company called **XYZQ**" in text and "Did you mean Xyz Ltd (**XYZ**)?" in text
    notes = entity_notes_text(
        {
            "entities": ["ETERNAL"],
            "entity_notes": ["Zomato Ltd is now listed as Eternal Ltd (ETERNAL)."],
            "entity_resolutions": [{"query": "FOO", "status": "not_found", "candidates": []}],
        }
    )
    assert "Eternal" in notes and "“FOO”" in notes


class TestLlmMentionInsideAResolvedName:
    """Live run 2026-10-18: "Relaince industries analysis" asked "Which Relaince do you mean?"."""

    def test_a_word_of_a_resolved_name_is_not_asked_about(self):
        from graph.entity_resolution import add_llm_mentions, resolve_mentions

        query = "Relaince industries analysis"
        result = add_llm_mentions(resolve_mentions(query), ["Relaince"], query)
        assert result.entities == ["RELIANCE"] and result.unresolved == []

    def test_a_different_company_the_model_found_is_still_added(self):
        from graph.entity_resolution import add_llm_mentions, resolve_mentions

        query = "Reliance results and its biggest IT rival"  # the model names a company the words only imply
        result = add_llm_mentions(resolve_mentions(query), ["Infosys"], query)
        assert set(result.entities) == {"RELIANCE", "INFY"}
