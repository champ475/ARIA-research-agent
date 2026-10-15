"""SEC-09: the checkpoint serializer never unpickles and never constructs
classes outside LangGraph's safe list — yet a full fan-out + HITL turn still
round-trips through it."""

from __future__ import annotations

import json
import pickle
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Interrupt, Send

from graph.checkpointer import make_serializer
from graph.graph import build_graph
from tests.graph.test_graph import _StubClient

_side_effects: list[str] = []


def _record_side_effect(tag: str) -> str:
    _side_effects.append(tag)
    return tag


class _Evil:
    """Unpickling this calls _record_side_effect — a stand-in for os.system."""

    def __reduce__(self):
        return (_record_side_effect, ("pickle-executed",))


def test_pickle_typed_payload_is_refused_without_running_it():
    _side_effects.clear()
    with pytest.raises(NotImplementedError):
        make_serializer().loads_typed(("pickle", pickle.dumps(_Evil())))
    assert _side_effects == []


def test_unpicklable_state_fails_loudly_instead_of_falling_back_to_pickle():
    with pytest.raises(Exception):  # noqa: B017 - ormsgpack's encode error
        make_serializer().dumps_typed({"oops": _Evil()})


def test_class_outside_the_allowlist_is_not_constructed():
    import pydantic
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    class Payload(pydantic.BaseModel):
        x: int = 1

    # Written by a permissive serializer (or by an attacker): names a class.
    blob = JsonPlusSerializer(allowed_msgpack_modules=True).dumps_typed({"p": Payload()})
    loaded = make_serializer().loads_typed(blob)
    assert loaded == {"p": {"x": 1}}  # plain data; Payload was never imported/instantiated


def test_langgraph_and_stdlib_safe_types_round_trip():
    from datetime import UTC, datetime
    from decimal import Decimal

    serde = make_serializer()
    value = {
        "sends": [Send("execute_task_node", {"task_id": "fetch_info:TCS"})],
        "interrupt": Interrupt(value={"type": "verification_gate"}),
        "when": datetime(2026, 10, 15, 9, 15, tzinfo=UTC),
        "amount": Decimal("1234.50"),
    }
    assert serde.loads_typed(serde.dumps_typed(value)) == value


def _graph(checkpointer):
    qa = _StubClient(json.dumps({"intent": "equity", "objective": "compare", "complexity": "complex"}))
    return build_graph(
        query_analyzer_client=qa,
        planner_client=_StubClient(json.dumps({"tools": ["fetch_info"]})),
        reflection_client=_StubClient(json.dumps({"missing": []})),
        verification_client=_StubClient(json.dumps({"issues": []})),
        communication_client=_StubClient("report"),
        checkpointer=checkpointer,
    )


def test_full_fan_out_and_hitl_turn_round_trips_through_the_strict_serializer(monkeypatch):
    """6 entities -> research_confirmation interrupt; approve -> fan-out of 6
    Send tasks -> completion. Every superstep is serialized with the strict
    serde (InMemorySaver stores serde.dumps_typed output)."""
    monkeypatch.setattr("graph.nodes.query_analyzer.MAX_ENTITIES_PER_TURN", 10)  # 6 companies (VAL-03)
    saver = InMemorySaver(serde=make_serializer())
    graph = _graph(saver)
    config = {"configurable": {"thread_id": "sec09-fanout"}}
    with (
        patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
    ):
        paused = graph.invoke(
            {"query": "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare", "max_loops": 2}, config
        )
        assert paused["__interrupt__"][0].value["type"] == "research_confirmation"
        # The paused state is re-read through the serializer, as a resume after a restart would.
        snapshot = graph.get_state(config)
        assert snapshot.next and snapshot.values["query"].startswith("RELIANCE")
        resumed = graph.invoke(Command(resume=True), config)
    assert len(resumed["tasks"]) == 6
    assert all(t["status"] == "done" for t in resumed["tasks"])
    assert resumed["pending_synthesis_prompt"]
