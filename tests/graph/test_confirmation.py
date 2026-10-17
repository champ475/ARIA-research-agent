"""VAL-08: lenient yes/no, a new question cancels a pending confirmation, and
only explicit report ids are looked up."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from graph.confirmation import classify_reply


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes!",
        "y",
        "sure",
        "Sure.",
        "go ahead",
        "yes please",
        "ok 👍",
        "okay thanks",
        "proceed",
        "yeah sure",
        "haan",
        "theek hai",
        "do it",
        "show anyway",
        "approve",
    ],
)
def test_yes(text):
    assert classify_reply(text) == "yes"


@pytest.mark.parametrize(
    "text", ["no", "No.", "nah", "nope", "cancel", "not now", "no thanks", "never mind", "nahi", "stop"]
)
def test_no(text):
    assert classify_reply(text) == "no"


@pytest.mark.parametrize(
    "text",
    [
        "what about INFY?",
        "show me TCS news instead",
        "compare HDFC Bank and ICICI",
        "INFY price",
        "kya hai TCS ka PE",
        "actually analyse Wipro fundamentals please",
    ],
)
def test_new_questions(text):
    assert classify_reply(text) == "new_question"


@pytest.mark.parametrize("text", ["hmm", "maybe", "", "🤔"])
def test_unclear(text):
    assert classify_reply(text) == "unclear"


def _orchestrator():
    from agents.graph_orchestrator import GraphOrchestrator

    with (
        patch("agents.graph_orchestrator.build_graph", return_value=MagicMock()),
        patch("agents.graph_orchestrator.get_turn_checkpointer", return_value=None),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
    ):
        orch = GraphOrchestrator(user_id=1)
    orch._awaiting_confirmation = True
    orch._trial_quota_exceeded = lambda: False
    return orch


def _drain(orch, message):
    return list(orch._process_stream(message, MagicMock()))


def test_unclear_reply_reasks_without_resuming():
    orch = _orchestrator()
    out = _drain(orch, "hmm")
    assert "yes" in out[0][0].lower() and orch._awaiting_confirmation
    orch._graph.invoke.assert_not_called()


def test_sure_resumes_with_approval():
    from langgraph.types import Command

    orch = _orchestrator()
    orch._graph.invoke.return_value = {"final_report": "done", "intent": "equity"}
    _drain(orch, "sure")
    cmd = orch._graph.invoke.call_args[0][0]
    assert isinstance(cmd, Command) and cmd.resume is True


def test_nah_resumes_with_decline():
    orch = _orchestrator()
    orch._graph.invoke.return_value = {"final_report": "declined", "intent": "equity"}
    _drain(orch, "nah")
    assert orch._graph.invoke.call_args[0][0].resume is False


def test_new_question_cancels_the_pending_step_and_runs_as_a_fresh_turn():
    orch = _orchestrator()
    old_thread = orch._thread_id
    orch._graph.invoke.return_value = {"final_report": "INFY answer", "intent": "equity"}
    with patch("agents.graph_orchestrator.check_input") as ci:
        ci.return_value = MagicMock(action="allow", caution=False)
        _drain(orch, "what about INFY?")
    first_arg = orch._graph.invoke.call_args[0][0]
    assert isinstance(first_arg, dict) and first_arg["query"] == "what about INFY?"
    assert orch._thread_id != old_thread and not orch._awaiting_confirmation


@pytest.mark.parametrize(
    ("query", "report_id"),
    [
        ("download report #12", "12"),
        ("download report 7", "7"),
        ("get report no. 3", "3"),
        ("download report for 2024", None),
        ("download the 2024 report", None),
    ],
)
def test_only_explicit_report_ids(query, report_id):
    from graph.nodes.communication_layer import _REPORT_ID_RE

    m = _REPORT_ID_RE.search(query)
    assert (m.group(1) if m else None) == report_id
