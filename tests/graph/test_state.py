from copy import deepcopy
from datetime import datetime, timezone

from graph.state import append_dedup, upsert_by_id


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "yfinance",
        "tool": "tools.analysis.fundamental.compute",
        "entity": "RELIANCE",
        "evidence_type": "fundamental_ratio",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(timezone.utc),
        "confidence": 0.9,
        "citation_key": "pe",
    }
    base.update(overrides)
    return base


def _task(**overrides):
    base = {
        "id": "task-1",
        "description": "Fetch fundamentals for RELIANCE",
        "tool": "tools.analysis.fundamental.compute",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
    base.update(overrides)
    return base


class TestAppendDedup:
    def test_appends_distinct_evidence(self):
        existing = [_evidence()]
        new = [_evidence(id="ev-2", citation_key="pb", payload={"pb": 3.1})]
        result = append_dedup(existing, new)
        assert len(result) == 2
        assert {e["id"] for e in result} == {"ev-1", "ev-2"}

    def test_drops_duplicate_source_entity_type_citation(self):
        existing = [_evidence()]
        # Same (source, entity, evidence_type, citation_key) tuple, different id/payload —
        # this is what a re-fetch inside a reflection loop looks like.
        duplicate = _evidence(id="ev-2", payload={"pe": 24.5})
        result = append_dedup(existing, [duplicate])
        assert len(result) == 1
        assert result[0]["id"] == "ev-1"  # first-seen wins

    def test_does_not_mutate_existing_list(self):
        existing = [_evidence()]
        existing_copy = deepcopy(existing)
        append_dedup(existing, [_evidence(id="ev-2", citation_key="pb")])
        assert existing == existing_copy

    def test_handles_empty_inputs(self):
        ev = _evidence()
        assert append_dedup([], []) == []
        assert append_dedup(None, [ev]) == [ev]
        assert append_dedup([ev], None) == [ev]


class TestUpsertById:
    def test_appends_new_task(self):
        existing = [_task()]
        new = [_task(id="task-2", entity="TCS")]
        result = upsert_by_id(existing, new)
        assert [t["id"] for t in result] == ["task-1", "task-2"]

    def test_updates_existing_task_status(self):
        existing = [_task(status="running")]
        new = [_task(status="done", result_evidence_ids=["ev-1"])]
        result = upsert_by_id(existing, new)
        assert len(result) == 1
        assert result[0]["status"] == "done"
        assert result[0]["result_evidence_ids"] == ["ev-1"]

    def test_reflection_loopback_does_not_reset_done_tasks(self):
        # This is the exact scenario the reducer exists for: Reflection appends
        # a brand-new task without touching tasks already marked done.
        existing = [_task(id="task-1", status="done"), _task(id="task-2", status="done")]
        new = [_task(id="task-3", description="Fetch quarterly earnings")]
        result = upsert_by_id(existing, new)
        assert len(result) == 3
        assert result[0]["status"] == "done"
        assert result[1]["status"] == "done"
        assert result[2]["id"] == "task-3"

    def test_preserves_insertion_order(self):
        existing = [_task(id="a"), _task(id="b")]
        new = [_task(id="a", status="done"), _task(id="c")]
        result = upsert_by_id(existing, new)
        assert [t["id"] for t in result] == ["a", "b", "c"]

    def test_handles_empty_inputs(self):
        task = _task()
        assert upsert_by_id([], []) == []
        assert upsert_by_id(None, [task]) == [task]
        assert upsert_by_id([task], None) == [task]
