from langgraph.types import Send

from graph.nodes.research_executor import (
    _build_context,
    _ready_tasks,
    _stuck_tasks,
    research_executor,
    route_after_research_executor,
)


def _task(**overrides):
    base = {
        "id": "t1",
        "description": "x",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
    base.update(overrides)
    return base


class TestReadyTasks:
    def test_no_dependencies_is_immediately_ready(self):
        tasks = [_task(id="a")]
        assert [t["id"] for t in _ready_tasks(tasks)] == ["a"]

    def test_waits_for_dependency_to_be_done(self):
        tasks = [_task(id="a", status="pending"), _task(id="b", depends_on=["a"])]
        # "a" has no deps of its own so it's ready; "b" must wait on "a".
        assert [t["id"] for t in _ready_tasks(tasks)] == ["a"]

    def test_ready_once_dependency_done(self):
        tasks = [_task(id="a", status="done"), _task(id="b", depends_on=["a"])]
        assert [t["id"] for t in _ready_tasks(tasks)] == ["b"]

    def test_already_done_or_failed_tasks_are_not_ready(self):
        tasks = [_task(id="a", status="done"), _task(id="b", status="failed")]
        assert _ready_tasks(tasks) == []

    def test_multiple_independent_tasks_all_ready_at_once(self):
        tasks = [_task(id="a"), _task(id="b"), _task(id="c")]
        assert {t["id"] for t in _ready_tasks(tasks)} == {"a", "b", "c"}


class TestStuckTasks:
    def test_pending_task_depending_on_failed_task_is_stuck(self):
        tasks = [_task(id="a", status="failed"), _task(id="b", depends_on=["a"])]
        assert [t["id"] for t in _stuck_tasks(tasks)] == ["b"]

    def test_pending_task_depending_on_pending_task_is_not_stuck(self):
        tasks = [_task(id="a", status="pending"), _task(id="b", depends_on=["a"])]
        assert _stuck_tasks(tasks) == []

    def test_no_stuck_tasks_when_all_healthy(self):
        tasks = [_task(id="a", status="done"), _task(id="b", depends_on=["a"], status="done")]
        assert _stuck_tasks(tasks) == []


class TestBuildContext:
    def test_maps_dependency_tool_name_to_its_evidence_payload(self):
        tasks_by_id = {
            "fetch_info:RELIANCE": _task(id="fetch_info:RELIANCE", tool="fetch_info"),
        }
        task = _task(id="compute_fundamentals:RELIANCE", tool="compute_fundamentals",
                      depends_on=["fetch_info:RELIANCE"])
        evidence = [{"tool": "fetch_info", "entity": "RELIANCE", "payload": {"pe": 24.3}}]
        context = _build_context(task, tasks_by_id, evidence)
        assert context["fetch_info"] == {"pe": 24.3}

    def test_missing_dependency_evidence_is_silently_skipped(self):
        tasks_by_id = {"fetch_info:RELIANCE": _task(id="fetch_info:RELIANCE", tool="fetch_info")}
        task = _task(id="compute_fundamentals:RELIANCE", depends_on=["fetch_info:RELIANCE"])
        context = _build_context(task, tasks_by_id, evidence=[])
        assert context == {"_user_id": None, "_query": None}

    def test_query_injected_regardless_of_dependencies(self):
        task = _task(id="screener_parse_query:market", tool="screener_parse_query", depends_on=[])
        context = _build_context(task, {}, evidence=[], query="top gainers today")
        assert context["_query"] == "top gainers today"

    def test_user_id_injected_regardless_of_dependencies(self):
        task = _task(id="alert_list:TCS", tool="alert_list", depends_on=[])
        context = _build_context(task, {}, evidence=[], user_id=42)
        assert context["_user_id"] == 42


class TestResearchExecutorNode:
    def test_marks_stuck_tasks_failed(self):
        tasks = [_task(id="a", status="failed"), _task(id="b", depends_on=["a"])]
        result = research_executor({"tasks": tasks})
        updated = {t["id"]: t for t in result["tasks"]}
        assert updated["b"]["status"] == "failed"

    def test_leaves_tasks_untouched_when_ready_tasks_exist(self):
        tasks = [_task(id="a")]
        result = research_executor({"tasks": tasks})
        assert "tasks" not in result

    def test_writes_journal_entry(self):
        tasks = [_task(id="a")]
        result = research_executor({"tasks": tasks})
        assert result["research_journal"][0]["node"] == "ResearchExecutor"


class TestRouteAfterResearchExecutor:
    def test_returns_evidence_summarizer_when_nothing_ready(self):
        tasks = [_task(id="a", status="done")]
        route = route_after_research_executor({"tasks": tasks, "evidence": []})
        assert route == "EvidenceSummarizer"

    def test_returns_send_list_for_ready_tasks(self):
        tasks = [_task(id="a"), _task(id="b")]
        route = route_after_research_executor({"tasks": tasks, "evidence": []})
        assert isinstance(route, list)
        assert all(isinstance(s, Send) for s in route)
        assert {s.node for s in route} == {"execute_task_node"}
        assert {s.arg["task"]["id"] for s in route} == {"a", "b"}

    def test_send_arg_includes_resolved_context(self):
        tasks = [
            _task(id="fetch_info:RELIANCE", tool="fetch_info", status="done"),
            _task(id="compute_fundamentals:RELIANCE", tool="compute_fundamentals",
                   depends_on=["fetch_info:RELIANCE"]),
        ]
        evidence = [{"tool": "fetch_info", "entity": "RELIANCE", "payload": {"pe": 24.3}}]
        route = route_after_research_executor({"tasks": tasks, "evidence": evidence})
        assert len(route) == 1
        assert route[0].arg["context"]["fetch_info"] == {"pe": 24.3}
