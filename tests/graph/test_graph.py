import json
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph.graph import build_graph


class _StubClient:
    def __init__(self, response: str):
        self._response = response

    def ask(self, prompt, **kwargs):
        return self._response


def _build(qa_response: dict, planner_response: dict = None, reflection_response: dict = None, verification_response: dict = None):
    qa = _StubClient(json.dumps(qa_response))
    planner = _StubClient(json.dumps(planner_response or {}))
    reflection = _StubClient(json.dumps(reflection_response if reflection_response is not None else {"missing": []}))
    verification = _StubClient(json.dumps(verification_response if verification_response is not None else {"issues": []}))
    communication = _StubClient("Stub final report text.")
    return build_graph(
        query_analyzer_client=qa, planner_client=planner,
        reflection_client=reflection, verification_client=verification,
        communication_client=communication,
    )


class TestMinimalGraphAssembly:
    def test_trivial_query_flows_through_with_no_tasks(self):
        graph = _build({"intent": "chat", "objective": "", "complexity": "simple"})
        result = graph.invoke({"query": "hello"})
        assert result["tasks"] == []
        assert "final_report" in result
        assert result["final_report"]

    def test_equity_query_generates_tasks_and_reaches_report(self):
        graph = _build(
            {"intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate"},
            {"tools": ["compute_fundamentals"]},
        )
        result = graph.invoke({"query": "how are RELIANCE fundamentals looking"})
        assert len(result["tasks"]) > 0
        assert result["intent"] == "equity"
        assert "final_report" in result

    def test_journal_accumulates_in_node_execution_order(self):
        graph = _build({"intent": "chat", "objective": "", "complexity": "simple"})
        result = graph.invoke({"query": "hi"})
        node_order = [entry["node"] for entry in result["research_journal"]]
        assert node_order == ["QueryAnalyzer", "Planner", "ConclusionBuilder", "CommunicationLayer"]

    def test_entities_extracted_before_planner_runs(self):
        graph = _build(
            {"intent": "equity", "objective": "analyse RELIANCE", "complexity": "moderate"},
            {"tools": ["fetch_info"]},
        )
        result = graph.invoke({"query": "what's the outlook on RELIANCE"})
        assert "RELIANCE" in result["entities"]
        assert any(t["entity"] == "RELIANCE" for t in result["tasks"])


class TestResearchExecutorEndToEnd:
    """Covers Phase 3: ResearchExecutor's Send fan-out actually resolves
    multi-wave dependencies (fetch_* before compute_fundamentals) and
    EvidenceSummarizer runs before the report stub."""

    def _run_with_mocked_tools(self):
        qa = _StubClient(json.dumps({
            "intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate",
        }))
        planner = _StubClient(json.dumps({"tools": ["compute_fundamentals"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Stub final report text.")
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}), \
             patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}), \
             patch("graph.tool_catalogue.compute_fundamental_ratios", return_value={"pe": 24.3}), \
             patch("tools.data.evidence_cache.evidence_cache_get", return_value=None), \
             patch("tools.data.evidence_cache.evidence_cache_put", return_value=None):
            graph = build_graph(
                query_analyzer_client=qa, planner_client=planner,
                reflection_client=reflection, verification_client=verification,
                communication_client=communication,
            )
            return graph.invoke({"query": "analyse RELIANCE fundamentals only"})

    def test_all_tasks_reach_done(self):
        result = self._run_with_mocked_tools()
        statuses = {t["status"] for t in result["tasks"]}
        assert statuses == {"done"}

    def test_dependency_task_only_resolves_after_its_dependencies(self):
        result = self._run_with_mocked_tools()
        node_order = [e["node"] for e in result["research_journal"]]
        first_compute_idx = None
        last_fetch_idx = None
        for i, entry in enumerate(result["research_journal"]):
            if entry["node"] == "execute_task":
                if "compute_fundamentals:RELIANCE" in entry["decision"] and first_compute_idx is None:
                    first_compute_idx = i
                if "fetch_info:RELIANCE" in entry["decision"] or "fetch_financials:RELIANCE" in entry["decision"]:
                    last_fetch_idx = i
        assert first_compute_idx is not None and last_fetch_idx is not None
        assert last_fetch_idx < first_compute_idx

    def test_evidence_summarizer_runs_before_report(self):
        result = self._run_with_mocked_tools()
        node_order = [e["node"] for e in result["research_journal"]]
        assert node_order.index("EvidenceSummarizer") < node_order.index("CommunicationLayer")

    def test_evidence_summary_populated(self):
        result = self._run_with_mocked_tools()
        assert "RELIANCE" in result["evidence_summary"]
        assert "fundamental_ratio" in result["evidence_summary"]["RELIANCE"]

    def test_full_pipeline_produces_conclusions_and_real_report(self):
        result = self._run_with_mocked_tools()
        assert result["conclusions"]
        assert any(c["citation_key"] == "pe" for c in result["conclusions"])
        assert result["final_report"] == "Stub final report text."
        assert result["confidence_summary"]


class TestReflectionVerificationLoop:
    """Covers Phase 4: the Reflection<->ResearchExecutor and
    Verification<->Reflection loops, and that max_loops actually bounds
    them — a Reflection that always claims something is missing must not
    loop forever."""

    def _run(self, reflection_response, verification_response=None, query="analyse RELIANCE fundamentals"):
        qa = _StubClient(json.dumps({
            "intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate",
        }))
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps(reflection_response))
        verification = _StubClient(json.dumps(verification_response or {"issues": []}))
        communication = _StubClient("Stub final report text.")
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}), \
             patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}), \
             patch("tools.data.evidence_cache.evidence_cache_get", return_value=None), \
             patch("tools.data.evidence_cache.evidence_cache_put", return_value=None):
            graph = build_graph(
                query_analyzer_client=qa, planner_client=planner,
                reflection_client=reflection, verification_client=verification,
                communication_client=communication,
            )
            return graph.invoke({"query": query, "max_loops": 2})

    def test_reflection_requesting_more_data_loops_back_to_research_executor(self):
        # Always claims fetch_financials is missing — reflection_client
        # returns the same canned response every call, so this alone would
        # loop forever without max_loops.
        result = self._run({
            "missing": [{"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}],
        })
        node_order = [e["node"] for e in result["research_journal"]]
        assert node_order.count("Reflection") >= 2  # looped at least once

    def test_infinite_gap_reporting_terminates_within_max_loops(self):
        result = self._run({
            "missing": [{"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}],
        })
        assert result["loop_count"] <= result["max_loops"]
        assert "final_report" in result and result["final_report"]

    def test_reaches_communication_layer_despite_unresolved_gaps(self):
        result = self._run({
            "missing": [{"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}],
        })
        node_order = [e["node"] for e in result["research_journal"]]
        assert node_order[-1] == "CommunicationLayer"

    def test_verification_issue_loops_back_to_reflection_then_pauses_for_review(self):
        # Reflection is always satisfied (no gaps from its own lens), but
        # Verification keeps finding a low-confidence issue — this is the
        # ping-pong case Verification's own loop_count increment exists for.
        # Once the loop budget is exhausted, VerificationGate (Phase 6) pauses
        # for human review instead of silently reaching CommunicationLayer.
        qa = _StubClient(json.dumps({
            "intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate",
        }))
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({
            "issues": [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}],
        }))
        communication = _StubClient("Stub final report text.")
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}), \
             patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}), \
             patch("tools.data.evidence_cache.evidence_cache_get", return_value=None), \
             patch("tools.data.evidence_cache.evidence_cache_put", return_value=None):
            graph = build_graph(
                query_analyzer_client=qa, planner_client=planner,
                reflection_client=reflection, verification_client=verification,
                communication_client=communication, checkpointer=InMemorySaver(),
            )
            config = {"configurable": {"thread_id": "test-thread"}}
            paused = graph.invoke({"query": "analyse RELIANCE fundamentals", "max_loops": 2}, config=config)
            assert "__interrupt__" in paused
            assert paused["__interrupt__"][0].value["type"] == "verification_gate"

            resumed = graph.invoke(Command(resume=True), config=config)

        node_order = [e["node"] for e in resumed["research_journal"]]
        assert node_order.count("Verification") >= 2
        assert resumed["loop_count"] <= resumed["max_loops"]
        assert node_order[-1] == "CommunicationLayer"
        assert resumed["final_report"] == "Stub final report text."


class TestHumanInTheLoopGates:
    """Covers Phase 6: ResearchConfirmation and VerificationGate actually
    pause the graph (via a real checkpointer) and resume correctly — both
    the approve and decline paths."""

    def _build_with_many_tasks(self):
        qa = _StubClient(json.dumps({
            "intent": "equity", "objective": "compare RELIANCE and TCS", "complexity": "complex",
        }))
        # 6 entities x 1 tool = 6 tasks, over the complex-gate threshold of 5.
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Comparison report text.")
        graph = build_graph(
            query_analyzer_client=qa, planner_client=planner,
            reflection_client=reflection, verification_client=verification,
            communication_client=communication, checkpointer=InMemorySaver(),
        )
        return graph

    def test_research_confirmation_pauses_for_complex_plan(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-1"}}
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
            paused = graph.invoke(
                {"query": "RELIANCE TCS INFY HDFC WIPRO ITC compare", "max_loops": 2}, config=config
            )
        assert "__interrupt__" in paused
        assert paused["__interrupt__"][0].value["type"] == "research_confirmation"

    def test_research_confirmation_approved_resumes_and_completes(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-2"}}
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}), \
             patch("tools.data.evidence_cache.evidence_cache_get", return_value=None), \
             patch("tools.data.evidence_cache.evidence_cache_put", return_value=None):
            graph.invoke({"query": "RELIANCE TCS INFY HDFC WIPRO ITC compare", "max_loops": 2}, config=config)
            resumed = graph.invoke(Command(resume=True), config=config)
        assert resumed["final_report"] == "Comparison report text."
        assert all(t["status"] == "done" for t in resumed["tasks"])

    def test_research_confirmation_declined_skips_research_entirely(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-3"}}
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
            graph.invoke({"query": "RELIANCE TCS INFY HDFC WIPRO ITC compare", "max_loops": 2}, config=config)
            resumed = graph.invoke(Command(resume=False), config=config)
        assert "declined" in resumed["final_report"]
        assert all(t["status"] == "skipped" for t in resumed["tasks"])
        assert "__interrupt__" not in resumed  # run completed, didn't pause again
