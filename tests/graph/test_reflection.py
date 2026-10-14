import json

from agents.llm_client import LLMProvidersExhausted
from graph.nodes.reflection import build_reflection_node


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.last_call_kwargs = None

    def ask(self, prompt, **kwargs):
        self.last_call_kwargs = kwargs
        return self._response


class _ExhaustedClient:
    """Raises LLMProvidersExhausted on every ask() call — simulates every
    configured LLM provider being unavailable."""

    def ask(self, prompt, **kwargs):
        raise LLMProvidersExhausted(Exception("all providers down"), is_byo=False)


def _base_state(**overrides):
    state = {
        "query": "how are RELIANCE fundamentals looking",
        "objective": "analyse RELIANCE fundamentals",
        "entities": ["RELIANCE"],
        "evidence_summary": {"RELIANCE": {"fundamental_ratio": {"summary": {"pe": 24.3}}}},
        "tasks": [],
        "loop_count": 0,
        "max_loops": 2,
    }
    state.update(overrides)
    return state


class TestReflectionNoGaps:
    def test_empty_missing_list_proceeds_without_new_tasks(self):
        stub = _StubClient(json.dumps({"missing": []}))
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert result["missing_info"] == []
        assert "tasks" not in result

    def test_does_not_increment_loop_count_when_satisfied(self):
        stub = _StubClient(json.dumps({"missing": []}))
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert "loop_count" not in result


class TestReflectionStructuredOutput:
    def test_parses_missing_item_shape(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {
                            "missing": "Quarterly earnings",
                            "reason": "Needed for revenue trend",
                            "priority": "high",
                            "action": "fetch_financials",
                        }
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state())
        item = result["missing_info"][0]
        assert item["missing"] == "Quarterly earnings"
        assert item["reason"] == "Needed for revenue trend"
        assert item["priority"] == "high"
        assert item["action"] == "fetch_financials"

    def test_generates_task_for_valid_action(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "high", "action": "fetch_financials"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert any(t["tool"] == "fetch_financials" and t["entity"] == "RELIANCE" for t in result["tasks"])

    def test_ignores_invalid_action_tool_name(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "high", "action": "not_a_real_tool"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert result["tasks"] == []

    def test_invalid_priority_falls_back_to_medium(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "urgent!!", "action": "fetch_financials"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert result["missing_info"][0]["priority"] == "medium"

    def test_falls_back_to_empty_on_unparseable_output(self):
        stub = _StubClient("not json")
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert result["missing_info"] == []


class TestReflectionLoopBudget:
    def test_increments_loop_count_when_requesting_more_work(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "high", "action": "fetch_financials"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state(loop_count=0))
        assert result["loop_count"] == 1

    def test_does_not_generate_tasks_when_loop_budget_exhausted(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "high", "action": "fetch_financials"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        result = node(_base_state(loop_count=2, max_loops=2))
        assert "tasks" not in result
        assert "loop_count" not in result
        assert result["missing_info"] != []

    def test_does_not_reset_already_done_tasks(self):
        stub = _StubClient(
            json.dumps(
                {
                    "missing": [
                        {"missing": "x", "reason": "y", "priority": "high", "action": "fetch_financials"}
                    ],
                }
            )
        )
        node = build_reflection_node(stub)
        existing = [
            {
                "id": "fetch_financials:RELIANCE",
                "description": "x",
                "tool": "fetch_financials",
                "entity": "RELIANCE",
                "depends_on": [],
                "status": "done",
                "result_evidence_ids": ["ev-1"],
            }
        ]
        result = node(_base_state(tasks=existing))
        assert result["tasks"] == []


class TestReflectionProviderExhaustion:
    """Bug fix: a total LLM-provider failure must never be conflated with
    "checked, found nothing" — the node must return without raising, and
    force the loop to terminate rather than route back to Reflection."""

    def test_returns_without_raising(self):
        node = build_reflection_node(_ExhaustedClient())
        result = node(_base_state())  # must not raise
        assert result["missing_info"] == []

    def test_forces_loop_count_to_max_loops(self):
        node = build_reflection_node(_ExhaustedClient())
        result = node(_base_state(loop_count=0, max_loops=2))
        # loop_count's reducer is Annotated[int, operator.add] — the raw
        # delta returned here, added to the current count, must land at
        # or above max_loops so the graph's loop_count < max_loops check
        # exits terminally.
        assert result["loop_count"] == 2

    def test_no_new_tasks_queued(self):
        node = build_reflection_node(_ExhaustedClient())
        result = node(_base_state())
        assert "tasks" not in result

    def test_writes_journal_entry_noting_exhaustion(self):
        node = build_reflection_node(_ExhaustedClient())
        result = node(_base_state())
        entry = result["research_journal"][0]
        assert entry["node"] == "Reflection"
        assert "exhausted" in entry["decision"].lower()


class TestReflectionRouting:
    def test_routes_through_fast_model(self):
        from config import GROQ_MODEL_FAST

        stub = _StubClient(json.dumps({"missing": []}))
        node = build_reflection_node(stub)
        node(_base_state())
        assert stub.last_call_kwargs["model"] == GROQ_MODEL_FAST

    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"missing": []}))
        node = build_reflection_node(stub)
        result = node(_base_state())
        assert result["research_journal"][0]["node"] == "Reflection"
