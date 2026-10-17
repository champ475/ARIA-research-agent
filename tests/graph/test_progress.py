"""UX-02: a research turn reports its steps while it runs; the routing badge
is off by default; a rejected BYO key says how to fix it."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from graph.progress import ProgressUpdate, labels_for, run_with_progress, task_label
from services import chat as chat_svc
from tests.graph.test_graph import _build
from ui import chat as chat_ui


def test_labels_for_nodes_and_tasks():
    assert labels_for({"QueryAnalyzer": {}}) == ["Understanding the question"]
    assert labels_for({"execute_task_node": {"tasks": [{"tool": "fetch_financials", "status": "done"}]}}) == [
        "Fetching financial statements"
    ]
    assert labels_for({"execute_task_node": {"tasks": [{"tool": "fetch_news", "status": "failed"}]}}) == []
    assert task_label("compute_fundamentals") == "Computing fundamental ratios"
    assert labels_for({"ResearchExecutor": {}}) == []


def test_a_real_equity_turn_reports_its_steps():
    graph = _build(
        {"intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate"},
        {"tools": ["compute_fundamentals"]},
    )
    with (
        patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
        patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}),
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
    ):
        gen = run_with_progress(graph, {"query": "how are RELIANCE fundamentals looking"}, {})
        labels = []
        try:
            while True:
                labels.append(next(gen).label)
        except StopIteration as stop:
            final = stop.value
    assert len(labels) >= 2
    assert labels[0] == "Understanding the question" and "Planning the research" in labels
    assert "Computing fundamental ratios" in labels and labels[-1] == "Writing the answer"
    assert final["intent"] == "equity" and final["pending_synthesis_prompt"]


def test_an_interrupt_is_returned_like_invoke():
    graph = MagicMock()
    interrupt = (MagicMock(value={"message": "Proceed?"}),)
    graph.stream.return_value = iter(
        [
            ("updates", {"QueryAnalyzer": {}}),
            ("values", {"intent": "equity"}),
            ("updates", {"__interrupt__": interrupt}),
        ]
    )
    gen = run_with_progress(graph, {}, {})
    assert next(gen) == ProgressUpdate("Understanding the question")
    try:
        next(gen)
    except StopIteration as stop:
        assert stop.value == {"intent": "equity", "__interrupt__": interrupt}


def test_orchestrator_yields_progress_before_the_answer_and_ttft_ignores_it():
    from tests.agents.test_graph_orchestrator import _build_orchestrator

    graph = MagicMock()
    graph.stream.return_value = iter(
        [
            ("updates", {"QueryAnalyzer": {}}),
            ("updates", {"Planner": {}}),
            ("values", {"final_report": "RELIANCE looks fine.", "intent": "report", "tasks": []}),
        ]
    )
    orch = _build_orchestrator(graph)
    with patch("utils.tracing._persist") as persist:
        out = list(orch.process_stream("analyse RELIANCE", progress=True))
    assert [o.label for o in out if isinstance(o, ProgressUpdate)] == [
        "Understanding the question",
        "Planning the research",
    ]
    assert out[-1][0].startswith("RELIANCE looks fine.")
    graph.invoke.assert_not_called()
    assert persist.called
    # Without progress (tests, the API path) it's a plain invoke, as before.
    graph.invoke.return_value = {"final_report": "x", "intent": "report", "tasks": []}
    assert not any(isinstance(o, ProgressUpdate) for o in orch.process_stream("again"))


def test_routing_badge_is_off_by_default_and_progress_is_escaped():
    assert chat_svc.routing_badge("equity") == ""
    with patch("config.SHOW_ROUTING_BADGE", True):
        assert chat_svc.routing_badge("equity").startswith("*🔀 Routed to:")
    html = chat_ui.progress_html(["Understanding the question", "<b>x</b>"])
    assert "&lt;b&gt;x&lt;/b&gt;…" in html and "Understanding the question →" in html
    assert 'role="status"' in html.replace("'", '"')


def test_rejected_byo_key_says_how_to_fix_it():
    from agents.llm_client import LLMProvidersExhausted, format_exhausted_message

    msg = format_exhausted_message(
        LLMProvidersExhausted(RuntimeError("Error code: 401 - invalid api key"), is_byo=True)
    )
    assert "API key was rejected" in msg and "Settings" in msg
