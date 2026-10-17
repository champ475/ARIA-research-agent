"""LLM-11: every call metered (tokens, latency, model, node, ₹ cost), fewer
calls per turn, and the trial token budget."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
from agents import llm_client as lc
from utils import tracing


def test_cost_uses_list_prices():
    # gpt-oss-20b: $0.075 in / $0.30 out per 1M tokens, at USD_INR
    cost = config.call_cost_inr("openai/gpt-oss-20b", 1_000_000, 1_000_000)
    assert cost == pytest.approx((0.075 + 0.30) * config.USD_INR)
    assert config.call_cost_inr("someone-elses-model", 10_000, 10_000) == 0.0


class _Provider(lc.ThreadLocalUsage):
    name = "Groq"

    def __init__(self, model="openai/gpt-oss-20b", usage=(1200, 300)):
        self.model, self._usage = model, usage

    def ask(self, prompt, temperature, max_tokens, model):
        self.last_usage = self._usage
        return "ok"

    def stream(self, prompt, temperature, max_tokens, model):
        yield "hel"
        yield "lo"
        self.last_usage = self._usage  # reported on the final chunk


def _client(node, provider, byo=False):
    c = lc.LLMClient.__new__(lc.LLMClient)
    c._system, c._prompt_version, c._node, c._is_byo = "s", "x@v1", node, byo
    c._primary, c._secondary, c._extra, c._local, c._active = provider, None, [], None, "Groq"
    return c


def test_each_call_is_metered_by_node_model_and_cost():
    with tracing.turn("t", 1, None, "q") as rec:
        _client("planner", _Provider()).ask("p")
        _client("planner", _Provider()).ask("p")
        "".join(_client("communication", _Provider("openai/gpt-oss-120b", (3000, 800))).stream("p"))
    assert rec.llm_calls == 3 and rec.tokens_in == 5400 and rec.tokens_out == 1400
    assert not rec.tokens_estimated  # the stream's final usage report was used
    assert rec.models == {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
    assert rec.llm_by_node["planner"]["calls"] == 2
    assert rec.llm_by_node["communication"]["tokens_out"] == 800
    expected = config.call_cost_inr("openai/gpt-oss-20b", 1200, 300) * 2 + config.call_cost_inr(
        "openai/gpt-oss-120b", 3000, 800
    )
    assert rec.cost_inr == pytest.approx(expected)


def test_byo_calls_cost_the_operator_nothing():
    with tracing.turn("t", 1, None, "q") as rec:
        _client("planner", _Provider(), byo=True).ask("p")
    assert rec.cost_inr == 0 and rec.llm_calls == 1


class _Stub:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def ask(self, prompt, **kwargs):
        self.calls += 1
        return self.reply


def test_moderate_equity_turn_needs_at_most_four_llm_calls():
    from graph.graph import build_graph

    stubs = {
        "qa": _Stub(json.dumps({"intent": "equity", "objective": "analyse TCS", "complexity": "moderate"})),
        "planner": _Stub(json.dumps({"tools": ["fetch_info"]})),
        "reflection": _Stub(json.dumps({"missing": []})),
        "verification": _Stub(json.dumps({"issues": []})),
        "communication": _Stub("answer"),
    }
    graph = build_graph(
        query_analyzer_client=stubs["qa"],
        planner_client=stubs["planner"],
        reflection_client=stubs["reflection"],
        verification_client=stubs["verification"],
        communication_client=stubs["communication"],
        checkpointer=InMemorySaver(),
    )
    with (
        patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 3500.0, "trailingPE": 28.0}),
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
    ):
        out = graph.invoke({"query": "analyse TCS", "max_loops": 2}, {"configurable": {"thread_id": "m1"}})
    graph_calls = sum(s.calls for s in stubs.values())
    synthesis = 1 if out.get("pending_synthesis_prompt") else 0  # streamed by the orchestrator
    assert stubs["reflection"].calls == 0  # every planned task succeeded
    assert graph_calls + synthesis <= 4


def test_trial_token_budget_blocks_before_spending_a_message(monkeypatch):
    from agents.graph_orchestrator import GraphOrchestrator

    with (
        patch("agents.graph_orchestrator.shared_graph", return_value=MagicMock()),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
    ):
        orch = GraphOrchestrator(user_id=5)
    monkeypatch.setattr("agents.graph_orchestrator.TRIAL_DAILY_TOKENS", 1000)
    with (
        patch("utils.db.trial_tokens_used", return_value=1000),
        patch("utils.db.trial_quota_check_and_increment") as quota,
    ):
        assert orch._trial_quota_exceeded() is True
    quota.assert_not_called()  # no message counted for a refused turn


def test_trial_turn_tokens_are_charged(monkeypatch):
    from agents.graph_orchestrator import GraphOrchestrator

    with (
        patch("agents.graph_orchestrator.shared_graph", return_value=MagicMock()),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
    ):
        orch = GraphOrchestrator(user_id=5)
    rec = tracing.TurnRecord(turn_id="t", user_id=5, session_id=None)
    rec.tokens_in, rec.tokens_out = 900, 100
    with patch("utils.db.trial_tokens_add") as add:
        orch._charge_trial_tokens(rec)
    assert add.call_args[0][:2] == (5, 1000)
    orch._provider_config = {"api_key": "k"}
    with patch("utils.db.trial_tokens_add") as add:
        orch._charge_trial_tokens(rec)
    add.assert_not_called()  # BYO usage isn't the operator's budget
