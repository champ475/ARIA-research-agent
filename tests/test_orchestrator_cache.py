"""REL-03 / VAL-10: bounded per-conversation orchestrator cache, shared
compiled graphs, and conversation state that survives eviction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache


class _FakeOrch:
    def __init__(self, user_id, session_id=None):
        self.user_id, self.session_id = user_id, session_id


@pytest.fixture
def fresh_cache(monkeypatch):
    from services import chat as chat_svc

    monkeypatch.setattr(chat_svc, "_orchestrators", TTLCache(maxsize=3, ttl=1800))
    with patch("agents.graph_orchestrator.GraphOrchestrator", _FakeOrch):
        yield chat_svc


def test_one_instance_per_conversation(fresh_cache):
    app = fresh_cache
    a1 = app.get_orchestrator(1, 10)
    assert app.get_orchestrator(1, 10) is a1
    assert app.get_orchestrator(1, 11) is not a1  # another tab / chat of the same user
    assert app.get_orchestrator(2, 10) is not a1


def test_cache_is_bounded(fresh_cache):
    app = fresh_cache
    for sid in range(10):
        app.get_orchestrator(1, sid)
    assert len(app._orchestrators) == 3


def test_evict_user_drops_only_that_users_conversations(fresh_cache):
    app = fresh_cache
    app.get_orchestrator(1, 10)
    app.get_orchestrator(1, 11)
    app.get_orchestrator(2, 10)
    app.evict_user(1)
    assert list(app._orchestrators.keys()) == [(2, 10)]


def test_compiled_graph_is_shared_per_provider_config():
    from agents import graph_orchestrator as go

    go.clear_graph_cache()
    with (
        patch.object(go, "build_graph", side_effect=lambda **kw: MagicMock()) as bg,
        patch.object(go, "get_turn_checkpointer", return_value=None),
    ):
        trial_a, trial_b = go.shared_graph(None), go.shared_graph(None)
        byo = go.shared_graph({"provider": "openai_compatible", "api_key": "k", "model": "m"})
    assert trial_a is trial_b and byo is not trial_a
    assert bg.call_count == 2
    go.clear_graph_cache()


def _orch(user_id, session_id, rows=(), pending=None):
    from agents.graph_orchestrator import GraphOrchestrator

    with (
        patch("agents.graph_orchestrator.shared_graph", return_value=MagicMock()),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
        patch("utils.db.chat_load", return_value=list(rows)),
        patch("utils.db.session_pending_get", return_value=pending),
    ):
        return GraphOrchestrator(user_id, session_id)


def test_rebuilt_conversation_restores_memory_without_the_routing_badge():
    rows = [
        {"role": "user", "content": "analyse TCS"},
        {"role": "assistant", "content": "*🔀 Routed to: 📈 Equity*\n\nTCS trades at 20x earnings."},
    ]
    orch = _orch(1, 10, rows)
    ctx = orch._memory.context()
    assert [m["content"] for m in ctx] == ["analyse TCS", "TCS trades at 20x earnings."]
    assert not orch._awaiting_confirmation


def test_rebuilt_conversation_resumes_a_pending_confirmation():
    orch = _orch(1, 10, pending="user-1-abc123")
    assert orch._awaiting_confirmation and orch._thread_id == "user-1-abc123"


def test_two_sessions_keep_separate_memory_and_confirmations():
    a = _orch(1, 10, pending="user-1-paused")
    b = _orch(1, 11)
    a._memory.add_user("tab A question")
    assert b._memory.context() == [] and not b._awaiting_confirmation
    assert a._awaiting_confirmation
