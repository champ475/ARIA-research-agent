import json
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph.entity_resolution import EntityResult
from graph.graph import build_graph


class _StubClient:
    def __init__(self, response: str):
        self._response = response

    def ask(self, prompt, **kwargs):
        return self._response


class _ExhaustedClient:
    """Raises LLMProvidersExhausted on every ask() call — simulates every
    configured LLM provider being unavailable."""

    def ask(self, prompt, **kwargs):
        from agents.llm_client import LLMProvidersExhausted

        raise LLMProvidersExhausted(Exception("all providers down"), is_byo=False)


def _build(
    qa_response: dict,
    planner_response: dict = None,
    reflection_response: dict = None,
    verification_response: dict = None,
):
    qa = _StubClient(json.dumps(qa_response))
    planner = _StubClient(json.dumps(planner_response or {}))
    reflection = _StubClient(
        json.dumps(reflection_response if reflection_response is not None else {"missing": []})
    )
    verification = _StubClient(
        json.dumps(verification_response if verification_response is not None else {"issues": []})
    )
    communication = _StubClient("Stub final report text.")
    return build_graph(
        query_analyzer_client=qa,
        planner_client=planner,
        reflection_client=reflection,
        verification_client=verification,
        communication_client=communication,
    )


class TestMinimalGraphAssembly:
    def test_trivial_query_flows_through_with_no_tasks(self):
        # intent="chat" is a streamable domain (graph/nodes/communication_layer.py
        # ::_is_streamable) — CommunicationLayer defers its LLM call to
        # GraphOrchestrator instead of populating final_report itself, so a
        # bare graph.invoke() (no GraphOrchestrator wrapping it) sees the
        # deferred prompt, not the finished text.
        graph = _build({"intent": "chat", "objective": "", "complexity": "simple"})
        result = graph.invoke({"query": "hello"})
        assert result["tasks"] == []
        assert result["pending_synthesis_prompt"]
        assert result["final_report"] == ""

    def test_equity_query_generates_tasks_and_reaches_report(self):
        # Mocked (previously hit real yfinance and could rate-limit into an
        # unrelated verification-gate HITL pause instead of ever reaching
        # CommunicationLayer — a pre-existing flake, not something this
        # test intends to exercise).
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
            result = graph.invoke({"query": "how are RELIANCE fundamentals looking"})
        assert len(result["tasks"]) > 0
        assert result["intent"] == "equity"
        assert "pending_synthesis_prompt" in result

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
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "objective": "analyse RELIANCE fundamentals",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["compute_fundamentals"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Stub final report text.")
        with (
            patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
            patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}),
            patch("graph.tool_catalogue.compute_fundamental_ratios", return_value={"pe": 24.3}),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            return graph.invoke({"query": "analyse RELIANCE fundamentals only"})

    def test_all_tasks_reach_done(self):
        result = self._run_with_mocked_tools()
        statuses = {t["status"] for t in result["tasks"]}
        assert statuses == {"done"}

    def test_dependency_task_only_resolves_after_its_dependencies(self):
        result = self._run_with_mocked_tools()
        [e["node"] for e in result["research_journal"]]
        first_compute_idx = None
        last_fetch_idx = None
        for i, entry in enumerate(result["research_journal"]):
            if entry["node"] == "execute_task":
                if "compute_fundamentals:RELIANCE" in entry["decision"] and first_compute_idx is None:
                    first_compute_idx = i
                if (
                    "fetch_info:RELIANCE" in entry["decision"]
                    or "fetch_financials:RELIANCE" in entry["decision"]
                ):
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
        # intent="equity" defers the LLM call (see test_trivial_query_flows_
        # through_with_no_tasks's comment) — the stub communication_client's
        # canned text is never called for a streamable domain, so this checks
        # the deferred prompt was built from the real conclusions instead.
        result = self._run_with_mocked_tools()
        assert result["conclusions"]
        assert any(c["citation_key"] == "pe" for c in result["conclusions"])
        assert result["final_report"] == ""
        assert "pe" in result["pending_synthesis_prompt"]
        assert result["confidence_summary"]


class TestReflectionVerificationLoop:
    """Covers Phase 4: the Reflection<->ResearchExecutor and
    Verification<->Reflection loops, and that max_loops actually bounds
    them — a Reflection that always claims something is missing must not
    loop forever."""

    def _run(self, reflection_response, verification_response=None, query="analyse RELIANCE fundamentals"):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "objective": "analyse RELIANCE fundamentals",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps(reflection_response))
        verification = _StubClient(json.dumps(verification_response or {"issues": []}))
        communication = _StubClient("Stub final report text.")
        with (
            patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
            patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            return graph.invoke({"query": query, "max_loops": 2})

    def test_reflection_requesting_more_data_loops_back_to_research_executor(self, monkeypatch):
        # LLM-11 skips Reflection's call when a moderate plan fully succeeded;
        # this test is about the loop itself, so make Reflection always run.
        monkeypatch.setattr("graph.nodes.reflection.REFLECTION_SKIP_WHEN_COMPLETE", False)
        # Always claims fetch_financials is missing — reflection_client
        # returns the same canned response every call, so this alone would
        # loop forever without max_loops.
        result = self._run(
            {
                "missing": [
                    {"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}
                ],
            }
        )
        node_order = [e["node"] for e in result["research_journal"]]
        assert node_order.count("Reflection") >= 2  # looped at least once

    def test_infinite_gap_reporting_terminates_within_max_loops(self):
        # intent="equity" (see this class's _run helper) defers the LLM call —
        # CommunicationLayer was still reached (that's what this test checks),
        # it just hands the prompt to GraphOrchestrator instead of resolving
        # final_report itself.
        result = self._run(
            {
                "missing": [
                    {"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}
                ],
            }
        )
        assert result["loop_count"] <= result["max_loops"]
        assert result["pending_synthesis_prompt"]

    def test_reaches_communication_layer_despite_unresolved_gaps(self):
        result = self._run(
            {
                "missing": [
                    {"missing": "financials", "reason": "x", "priority": "high", "action": "fetch_financials"}
                ],
            }
        )
        node_order = [e["node"] for e in result["research_journal"]]
        assert node_order[-1] == "CommunicationLayer"

    def test_verification_issue_loops_back_to_reflection_then_pauses_for_review(self):
        # Reflection is always satisfied (no gaps from its own lens), but
        # Verification keeps finding a low-confidence issue — this is the
        # ping-pong case Verification's own loop_count increment exists for.
        # Once the loop budget is exhausted, VerificationGate (Phase 6) pauses
        # for human review instead of silently reaching CommunicationLayer.
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "objective": "analyse RELIANCE fundamentals",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(
            json.dumps(
                {
                    "issues": [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}],
                }
            )
        )
        communication = _StubClient("Stub final report text.")
        with (
            patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
            patch("graph.tool_catalogue.get_financials", return_value={"totalRevenue": 900000}),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
                checkpointer=InMemorySaver(),
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
        # intent="equity" defers the LLM call (see module's other tests) —
        # CommunicationLayer was reached, it just handed off the prompt.
        assert resumed["pending_synthesis_prompt"]


class TestHumanInTheLoopGates:
    """Covers Phase 6: ResearchConfirmation and VerificationGate actually
    pause the graph (via a real checkpointer) and resume correctly — both
    the approve and decline paths."""

    @pytest.fixture(autouse=True)
    def _allow_six_companies(self, monkeypatch):
        # These use 6 companies to build a >5-task plan; VAL-03 would
        # otherwise stop at its 5-company cap before the gate is reached.
        monkeypatch.setattr("graph.nodes.query_analyzer.MAX_ENTITIES_PER_TURN", 10)

    def _build_with_many_tasks(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "objective": "compare RELIANCE and TCS",
                    "complexity": "complex",
                }
            )
        )
        # 6 entities x 1 tool = 6 tasks, over the complex-gate threshold of 5.
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Comparison report text.")
        graph = build_graph(
            query_analyzer_client=qa,
            planner_client=planner,
            reflection_client=reflection,
            verification_client=verification,
            communication_client=communication,
            checkpointer=InMemorySaver(),
        )
        return graph

    def test_research_confirmation_pauses_for_complex_plan(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-1"}}
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
            paused = graph.invoke(
                {"query": "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare", "max_loops": 2}, config=config
            )
        assert "__interrupt__" in paused
        assert paused["__interrupt__"][0].value["type"] == "research_confirmation"

    def test_research_confirmation_approved_resumes_and_completes(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-2"}}
        with (
            patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph.invoke(
                {"query": "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare", "max_loops": 2}, config=config
            )
            resumed = graph.invoke(Command(resume=True), config=config)
        # intent="equity" defers the LLM call to GraphOrchestrator (see other
        # tests in this module) — the stub's canned text is never called.
        assert resumed["pending_synthesis_prompt"]
        assert all(t["status"] == "done" for t in resumed["tasks"])

    def test_research_confirmation_declined_skips_research_entirely(self):
        graph = self._build_with_many_tasks()
        config = {"configurable": {"thread_id": "rc-test-3"}}
        with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
            graph.invoke(
                {"query": "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare", "max_loops": 2}, config=config
            )
            resumed = graph.invoke(Command(resume=False), config=config)
        assert "declined" in resumed["final_report"]
        assert all(t["status"] == "skipped" for t in resumed["tasks"])
        assert "__interrupt__" not in resumed  # run completed, didn't pause again


class TestProviderExhaustionTermination:
    """Bug fix: when every LLM provider is unavailable, Verification's
    llm_judge stage must reach a genuinely terminal state (a normal
    verification_gate pause, same as any other unresolved-issue case) in
    ONE pass — not loop back to Reflection repeatedly retrying a call that
    cannot succeed."""

    def test_reaches_verification_gate_without_looping_back_to_reflection(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "objective": "analyse RELIANCE",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        reflection = _StubClient(json.dumps({"missing": []}))  # stage 1/2 clean, no gaps
        verification = _ExhaustedClient()  # every provider down at the llm_judge stage
        communication = _StubClient("Stub final report text.")
        graph = build_graph(
            query_analyzer_client=qa,
            planner_client=planner,
            reflection_client=reflection,
            verification_client=verification,
            communication_client=communication,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "exhaustion-test-1"}}
        with (
            patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            paused = graph.invoke({"query": "analyse RELIANCE", "max_loops": 2}, config=config)

        assert "__interrupt__" in paused
        assert paused["__interrupt__"][0].value["type"] == "verification_gate"
        node_order = [entry["node"] for entry in paused["research_journal"]]
        assert node_order.count("Verification") == 1  # never looped back to Reflection
        assert node_order.count("Reflection") == 1  # the initial pass only


class TestReportDomainEndToEnd:
    """Phase 7.1: report intent flows through the same graph, selecting
    fetch_quarterly_results (a report-only tool) and producing pdf_path/
    md_path in the terminal state via CommunicationLayer's report branch."""

    def _run(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "objective": "initiating coverage on RELIANCE",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_quarterly_results"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Full IC report body.")
        with (
            patch("graph.tool_catalogue.get_latest_quarterly_results", return_value="Revenue up 10% YoY."),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
            patch(
                "graph.nodes.communication_layer.render_report", return_value=("/tmp/r.md", "/tmp/r.pdf")
            ) as mock_save,
            patch("graph.nodes.communication_layer.report_save") as mock_persist,
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke(
                {
                    "query": "initiating coverage report on RELIANCE",
                    "max_loops": 2,
                    "user_id": 7,
                }
            )
        return result, mock_save, mock_persist

    def test_report_query_selects_report_only_tool(self):
        result, _, _ = self._run()
        assert any(t["tool"] == "fetch_quarterly_results" for t in result["tasks"])
        assert all(t["status"] == "done" for t in result["tasks"])

    def test_report_query_produces_file_paths(self):
        result, mock_save, _ = self._run()
        mock_save.assert_called_once()
        assert result["md_path"] == "/tmp/r.md"
        assert result["pdf_path"] == "/tmp/r.pdf"

    def test_report_query_persists_to_reports_table(self):
        _, _, mock_persist = self._run()
        mock_persist.assert_called_once()
        assert mock_persist.call_args[0][0] == 7  # user_id threaded through GraphState

    def test_report_query_final_report_from_stub(self):
        result, _, _ = self._run()
        assert result["final_report"].startswith("Full IC report body.")
        assert "not a SEBI-registered research analyst" in result["final_report"]


class TestMfDomainEndToEnd:
    """Phase 7.2: mf intent selects mf-only tools (fetch_nav) and produces a
    match-count conclusion CommunicationLayer can report on."""

    def test_mf_query_resolves_through_the_graph(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "mf",
                    "objective": "find HDFC Flexicap fund NAV",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_nav"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Here's the NAV data you asked for.")
        import pandas as pd

        fake_df = pd.DataFrame({"scheme_name": ["HDFC Flexicap Fund"], "nav": [45.2]})
        with (
            patch("graph.tool_catalogue.get_all_nav", return_value=pd.DataFrame()),
            patch("graph.tool_catalogue.search_fund", return_value=fake_df),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke({"query": "what is the NAV of HDFC Flexicap fund", "max_loops": 2})
        assert any(t["tool"] == "fetch_nav" and t["status"] == "done" for t in result["tasks"])
        assert result["conclusions"]
        assert result["final_report"] == "Here's the NAV data you asked for."


class TestAlertDomainEndToEnd:
    """Phase 7.2: alert intent — CRUD-only, no data-fetch tools, resolves
    entirely against utils/db.py's alert helpers (mocked here)."""

    def test_alert_list_query_resolves_through_the_graph(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "alert",
                    "objective": "list my alerts",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["alert_list"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("You have 2 alerts.")
        with (
            patch("graph.tool_catalogue.db_alert_list", return_value=[{"id": 1}, {"id": 2}]),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            # QueryAnalyzer's entity extraction won't find a ticker in "TCS"-free
            # text; the query below names TCS purely so Planner has an entity
            # to cross-product against (see tool_catalogue.py's module docstring
            # re: entity-less alert queries being a known follow-up).
            result = graph.invoke({"query": "list my TCS alerts", "max_loops": 2, "user_id": 9})
        assert any(t["tool"] == "alert_list" and t["status"] == "done" for t in result["tasks"])
        assert result["conclusions"][0]["citation_key"] == "alerts.count"
        assert result["final_report"] == "You have 2 alerts."


class TestScreenerDomainEndToEnd:
    """Phase 7.3: screener intent has no ticker to extract (whole-market
    screen) — Planner falls back to the "market" sentinel entity, and the
    full comparison table reaches CommunicationLayer as one Conclusion (the
    RFC §18 cross-entity fix — see conclusion_builder.py's module comment)."""

    def test_screener_query_uses_market_sentinel_and_renders_table(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "screener",
                    "objective": "screen IT stocks with PE below 25",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["screener_to_markdown"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Here are 2 IT stocks under PE 25.")
        with (
            patch("graph.nodes.query_analyzer.resolve_mentions", side_effect=lambda q: EntityResult()),
            patch("graph.tool_catalogue.screener_parse_query", return_value={"max_pe": 25}),
            patch("graph.tool_catalogue.run_screener", return_value=[{"symbol": "TCS"}, {"symbol": "INFY"}]),
            patch("graph.tool_catalogue.screener_to_markdown", return_value="| TCS |\n| INFY |"),
            patch("graph.nodes.communication_layer.save_screener_csv", return_value="/tmp/s.csv"),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke({"query": "screen IT stocks with PE below 25", "max_loops": 2})
        assert all(t["entity"] == "market" for t in result["tasks"])
        assert all(t["status"] == "done" for t in result["tasks"])
        assert any(c["citation_key"] == "market.screener_to_markdown" for c in result["conclusions"])
        assert result["csv_path"] == "/tmp/s.csv"
        assert result["final_report"] == "Here are 2 IT stocks under PE 25."


class TestPlanningDomainEndToEnd:
    """Phase 7.3: planning intent bypasses Planner/ResearchExecutor entirely
    (see communication_layer.py's module docstring) — QueryAnalyzer still
    runs, but Planner sees intent has nothing it can plan tools for."""

    def test_incomplete_intake_short_circuits_before_conclusion_builder(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "planning",
                    "objective": "get financial plan advice",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": []}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("unused")  # intake_turn call is separately mocked below
        with (
            patch("graph.nodes.query_analyzer.resolve_mentions", side_effect=lambda q: EntityResult()),
            patch("graph.nodes.communication_layer.profile_get", return_value={}),
            patch(
                "graph.nodes.communication_layer._planning_intake_turn",
                return_value=({"age": 30}, False, "Tell me your capital and goal too"),
            ),
            patch("graph.nodes.communication_layer.profile_upsert", return_value={"age": 30}),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke({"query": "I'm 30 years old", "max_loops": 2, "user_id": 5})
        assert result["final_report"] == "Tell me your capital and goal too"
        assert result["tasks"] == []

    def test_complete_intake_builds_and_returns_strategy(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "planning",
                    "objective": "build my plan",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": []}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("unused")
        complete_profile = {
            "age": 30,
            "capital": 1000000,
            "horizon_years": 20,
            "goal": "retirement",
            "risk_tolerance": "moderate",
        }
        with (
            patch("graph.nodes.query_analyzer.resolve_mentions", side_effect=lambda q: EntityResult()),
            patch("graph.nodes.communication_layer.profile_get", return_value={}),
            patch(
                "graph.nodes.communication_layer._planning_intake_turn",
                return_value=(complete_profile, True, "Building now"),
            ),
            patch("graph.nodes.communication_layer.profile_upsert", return_value=complete_profile),
            patch("graph.nodes.communication_layer._planning_build_strategy", return_value="Full strategy"),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke(
                {"query": "here's everything you need, build my plan", "max_loops": 2, "user_id": 5}
            )
        assert result["final_report"] == "Full strategy"


class TestMultiIntentEndToEnd:
    """Phase 7.4: a single message naming two domains produces one merged
    answer through one graph.invoke() call, with both domains' side effects
    (report file output here) firing — see communication_layer.py's
    _intents() helper."""

    def test_report_plus_alert_secondary_intent_writes_report_file(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "secondary_intents": ["alert"],
                    "objective": "IC report on RELIANCE and alert if it drops 5%",
                    "complexity": "complex",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["fetch_quarterly_results"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Report + alert note.")
        with (
            patch("graph.tool_catalogue.get_latest_quarterly_results", return_value="Revenue up 10%."),
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
            patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/r.md", "/tmp/r.pdf")),
            patch("graph.nodes.communication_layer.report_save"),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke(
                {
                    "query": "IC report on RELIANCE and alert me if it drops 5%",
                    "max_loops": 2,
                    "user_id": 3,
                }
            )
        assert result["intent"] == "report"
        assert result["secondary_intents"] == ["alert"]
        assert result["md_path"] == "/tmp/r.md"
        assert result["final_report"].startswith("Report + alert note.")


class TestAlertCancelQuery:
    def test_alert_cancel_query_removes_matching_alert(self):
        qa = _StubClient(
            json.dumps(
                {
                    "intent": "alert",
                    "objective": "cancel my TCS alert",
                    "complexity": "moderate",
                }
            )
        )
        planner = _StubClient(json.dumps({"tools": ["alert_cancel"]}))
        reflection = _StubClient(json.dumps({"missing": []}))
        verification = _StubClient(json.dumps({"issues": []}))
        communication = _StubClient("Your TCS alert has been cancelled.")
        with (
            patch(
                "graph.tool_catalogue.db_alert_list",
                return_value=[{"id": 7, "symbol": "TCS", "active": True}],
            ),
            patch("graph.tool_catalogue.db_alert_remove") as mock_remove,
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            graph = build_graph(
                query_analyzer_client=qa,
                planner_client=planner,
                reflection_client=reflection,
                verification_client=verification,
                communication_client=communication,
            )
            result = graph.invoke({"query": "cancel my TCS alert", "max_loops": 2, "user_id": 9})
        mock_remove.assert_called_once_with(9, 7)
        assert all(t["status"] == "done" for t in result["tasks"])
        assert result["final_report"] == "Your TCS alert has been cancelled."
