"""LLM-03: every figure in an answer must trace to the text the model was
given — exactly, within rounding, after a unit change, or as a simple
derivation — or it is regenerated once and then flagged."""

from unittest.mock import MagicMock, patch

import pytest

from graph.grounding import check, extract_figures

SOURCE = """Verified conclusions:
- TCS pe = 24.31 [^pe]
- TCS currentPrice = 3450.75
- TCS previousClose = 3286.4
- TCS revenue = 642590000000
Evidence data:
Revenue from operations 64,259 crore; Net profit 11,909 crore. BFSI 31.2% of revenue. ROE 0.153
User: alert me if TCS falls below 3,500"""


@pytest.mark.parametrize(
    "figure",
    [
        "₹3,450.75",
        "₹3,451",  # rounding
        "24.3",  # last displayed digit
        "24",
        "₹64,259 crore",
        "₹0.64 lakh crore",  # crore -> lakh crore
        "₹64,259 cr",  # crore spelled "cr"
        "15.3%",  # ratio -> percent
        "31.2%",
        "5%",  # % change 3286.4 -> 3450.75
        "₹164.35",  # difference
        "₹3,500",  # the user's own number
    ],
)
def test_supported_figures(figure):
    assert check(f"The figure is {figure}.", SOURCE).unsupported == []


@pytest.mark.parametrize(
    "figure", ["₹5,000", "a P/E of 99", "ROE of 22.5%", "₹1,200 crore", "up 12%", "₹6,42,590 crore"]
)  # the last is 10x the real revenue
def test_invented_figures_are_flagged(figure):
    result = check(f"Note: {figure}.", SOURCE)
    assert result.unsupported and result.checked == 1


@pytest.mark.parametrize(
    "text",
    [
        "Results for Q2 FY27 were filed on 14 Oct 2026.",
        "RSI(14) is near its 50-day average and the 200 DMA.",
        "1. First point\n2. Second point",
        "The ratio is cited [^pe] here.",
        "Alert #12 was created in 2025.",
        "There are 3 funds and 7 headlines.",
        "Over a 5-year horizon and 10 years of data.",
    ],
)
def test_dates_ordinals_periods_and_counts_are_not_checked(text):
    assert check(text, "").checked == 0


def test_figure_extraction_keeps_units():
    raws = [f.raw for f in extract_figures("₹1.2 lakh and 2 crore and 15.5% and 3.5x")]
    assert raws == ["₹1.2 lakh", "2 crore", "15.5%", "3.5x"]


def test_planning_mode_ignores_allocation_percentages_but_checks_returns():
    plan = "Put 60% in equity and 30% in debt. Large caps returned 18% CAGR last year."
    assert check(plan, "large cap funds data", mode="planning").unsupported == ["18%"]


# ── wiring ────────────────────────────────────────────────────────────────────


class _Seq:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def ask(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _conclusion():
    return {
        "claim": "TCS pe = 24.31",
        "value": 24.31,
        "confidence": 1.0,
        "evidence_ids": ["e"],
        "citation_key": "pe",
    }


def _sync_state():
    return {
        "intent": "report",
        "query": "IC report on TCS",
        "objective": "x",
        "conclusions": [_conclusion()],
        "entities": [],
    }


def test_stub_inventing_pe_99_gets_regenerated_then_flagged():
    from graph.nodes.communication_layer import build_communication_layer_node
    from utils import tracing

    client = _Seq("TCS trades at a P/E of 99.", "TCS trades at a P/E of 99 still.")
    with patch("utils.db.turn_record") as row, tracing.turn("ab" * 16, 1, None, "q") as rec:
        out = build_communication_layer_node(client)(_sync_state())
        rec.status = "ok"
    assert len(client.prompts) == 2 and "CORRECTION" in client.prompts[1] and "99" in client.prompts[1]
    assert "⚠️ *Unverified figures: 99" in out["final_report"]
    assert row.call_args.kwargs["grounding"] == {"checked": 1, "unsupported": ["99"], "regenerated": True}


def test_regeneration_that_fixes_the_figure_leaves_no_note():
    from graph.nodes.communication_layer import build_communication_layer_node

    client = _Seq("TCS trades at a P/E of 99.", "TCS trades at a P/E of 24.31.")
    out = build_communication_layer_node(client)(_sync_state())
    assert "Unverified" not in out["final_report"] and "24.31" in out["final_report"]


def test_streamed_answer_gets_the_note_at_the_end():
    from agents.graph_orchestrator import GraphOrchestrator

    graph = MagicMock()
    graph.invoke.return_value = {
        "pending_synthesis_prompt": "Verified conclusions:\n- TCS pe = 24.31",
        "intent": "equity",
        "tasks": [{"id": "t", "status": "done"}],
    }
    with (
        patch("agents.graph_orchestrator.build_graph", return_value=graph),
        patch("agents.graph_orchestrator.get_turn_checkpointer", return_value=None),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
    ):
        orch = GraphOrchestrator(user_id=1)
    orch._trial_quota_exceeded = lambda: False
    fake_client = MagicMock()
    fake_client.stream.return_value = iter(["TCS has a P/E ", "of 99 today."])
    with patch("agents.llm_client.LLMClient", return_value=fake_client), patch("utils.db.turn_record"):
        final = list(orch.process_stream("TCS pe?"))[-1][0]
    assert "TCS has a P/E of 99 today." in final and "Unverified figures: 99" in final
