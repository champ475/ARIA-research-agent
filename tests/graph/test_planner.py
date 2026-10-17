import json

from graph.nodes.planner import _build_tasks, build_planner_node
from graph.tool_catalogue import NO_ENTITY_SENTINEL


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.last_call_kwargs = None

    def ask(self, prompt, **kwargs):
        self.last_call_kwargs = kwargs
        return self._response


class TestBuildTasksEntitySentinel:
    """Locks in the interaction the alert_create entity-validation fix
    depends on: no entities at all (e.g. QueryAnalyzer's
    filter_valid_entities found nothing valid) still produces exactly one
    task per tool, tagged NO_ENTITY_SENTINEL — which alert_create's guard
    (graph/tool_catalogue.py) then refuses to write to the DB, rather than
    Planner silently skipping the tool."""

    def test_no_entities_produces_sentinel_tagged_task(self):
        tasks = _build_tasks(["alert_create"], [])
        assert len(tasks) == 1
        assert tasks[0]["entity"] == NO_ENTITY_SENTINEL
        assert tasks[0]["id"] == f"alert_create:{NO_ENTITY_SENTINEL}"

    def test_real_entity_produces_one_task_per_entity(self):
        tasks = _build_tasks(["alert_create"], ["INFY"])
        assert len(tasks) == 1
        assert tasks[0]["entity"] == "INFY"


class TestPlannerTrivialPath:
    def test_simple_complexity_short_circuits_to_no_tasks(self):
        node = build_planner_node(_StubClient("{}"))
        result = node({"objective": "what does PE mean", "entities": [], "complexity": "simple"})
        assert result["tasks"] == []
        assert result["plan"]["tasks"] == []

    def test_no_entities_but_no_tools_selected_produces_no_tasks(self):
        # Phase 7.3: entities=[] no longer short-circuits by itself (see
        # planner.py's module docstring) — this now reaches the LLM, which
        # in this stub selects zero tools, so there's still nothing to plan.
        node = build_planner_node(_StubClient("{}"))
        result = node({"objective": "explain RSI", "entities": [], "complexity": "moderate"})
        assert result["tasks"] == []

    def test_trivial_path_never_calls_llm(self):
        stub = _StubClient("{}")
        node = build_planner_node(stub)
        node({"objective": "hi", "entities": [], "complexity": "simple"})
        assert stub.last_call_kwargs is None

    def test_no_entities_with_selected_tools_uses_market_sentinel_entity(self):
        # Phase 7.3: whole-market/user-scoped domains (screener filter-based
        # screens, alert-list, mf category/news) have no ticker to extract but
        # still need to run a tool — Planner falls back to a synthetic
        # "market" entity so these tools still get exactly one Task.
        stub = _StubClient(json.dumps({"tools": ["fetch_top_movers"]}))
        node = build_planner_node(stub)
        result = node({"objective": "top gainers today", "entities": [], "complexity": "moderate"})
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["entity"] == "market"

    def test_company_bound_tools_are_not_run_on_the_market_sentinel(self):
        # Smoke test 2026-10-17: "what about its debt?" ran fetch_financials on "market".
        stub = _StubClient(json.dumps({"tools": ["fetch_financials", "fetch_news"]}))
        node = build_planner_node(stub)
        result = node({"objective": "its debt", "entities": [], "complexity": "moderate"})
        assert result["tasks"] == []

    def test_market_wide_tools_survive_alongside_dropped_company_tools(self):
        stub = _StubClient(json.dumps({"tools": ["fetch_financials", "fetch_top_movers"]}))
        node = build_planner_node(stub)
        result = node({"objective": "movers", "entities": [], "complexity": "moderate"})
        assert [t["tool"] for t in result["tasks"]] == ["fetch_top_movers"]

    def test_company_tools_still_run_for_each_named_company(self):
        tasks = _build_tasks(["fetch_info"], ["TCS", "INFY"])
        assert sorted(t["entity"] for t in tasks) == ["INFY", "TCS"]


class TestPlannerTaskGeneration:
    def test_generates_tasks_for_selected_tools(self):
        stub = _StubClient(json.dumps({"tools": ["compute_fundamentals"]}))
        node = build_planner_node(stub)
        result = node(
            {"objective": "analyse RELIANCE fundamentals", "entities": ["RELIANCE"], "complexity": "moderate"}
        )
        tool_names = {t["tool"] for t in result["tasks"]}
        # compute_fundamentals depends on fetch_info + fetch_financials — must be auto-included
        assert tool_names == {"compute_fundamentals", "fetch_info", "fetch_financials"}

    def test_dependency_task_ids_match_generated_task_ids(self):
        stub = _StubClient(json.dumps({"tools": ["compute_fundamentals"]}))
        node = build_planner_node(stub)
        result = node({"objective": "x", "entities": ["RELIANCE"], "complexity": "moderate"})
        tasks_by_id = {t["id"]: t for t in result["tasks"]}
        compute_task = tasks_by_id["compute_fundamentals:RELIANCE"]
        assert set(compute_task["depends_on"]) == {"fetch_info:RELIANCE", "fetch_financials:RELIANCE"}
        for dep_id in compute_task["depends_on"]:
            assert dep_id in tasks_by_id

    def test_generates_one_task_set_per_entity(self):
        stub = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        node = build_planner_node(stub)
        result = node({"objective": "compare", "entities": ["RELIANCE", "TCS"], "complexity": "complex"})
        entities_covered = {t["entity"] for t in result["tasks"]}
        assert entities_covered == {"RELIANCE", "TCS"}
        assert len(result["tasks"]) == 2

    def test_ignores_unknown_tool_names_from_llm(self):
        stub = _StubClient(json.dumps({"tools": ["fetch_info", "made_up_tool"]}))
        node = build_planner_node(stub)
        result = node({"objective": "x", "entities": ["RELIANCE"], "complexity": "moderate"})
        assert {t["tool"] for t in result["tasks"]} == {"fetch_info"}

    def test_falls_back_to_no_tasks_on_unparseable_output(self):
        stub = _StubClient("not json")
        node = build_planner_node(stub)
        result = node({"objective": "x", "entities": ["RELIANCE"], "complexity": "moderate"})
        assert result["tasks"] == []

    def test_routes_through_its_node_model_route(self):
        """LLM-08: the node's client is built for its own model route (the
        fast class) instead of passing a Groq-only model name per call."""
        from unittest.mock import patch

        with patch("graph.nodes.planner.LLMClient") as client_cls:
            build_planner_node(None, provider_config=None)
        assert client_cls.call_args.kwargs["node"] == "planner"

    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"tools": ["fetch_info"]}))
        node = build_planner_node(stub)
        result = node({"objective": "x", "entities": ["RELIANCE"], "complexity": "moderate"})
        assert result["research_journal"][0]["node"] == "Planner"


class TestSingleTaskTools:
    def test_market_wide_tools_run_once_whatever_companies_are_named(self):
        tasks = _build_tasks(["fetch_info", "fetch_market_overview", "fetch_top_movers"], ["TCS", "INFY"])
        by_tool = {}
        for t in tasks:
            by_tool.setdefault(t["tool"], []).append(t["entity"])
        assert sorted(by_tool["fetch_info"]) == ["INFY", "TCS"]
        assert by_tool["fetch_market_overview"] == ["market"] and by_tool["fetch_top_movers"] == ["market"]

    def test_the_portfolio_tool_still_runs_once(self):
        tasks = _build_tasks(["portfolio_view"], ["TCS"])
        assert [(t["tool"], t["entity"]) for t in tasks] == [("portfolio_view", "market")]


class TestScreenerChain:
    """Live smoke run 2026-10-18: the model picked screener_parse_query + screener_run but not
    screener_to_markdown, so the answer was "I couldn't find enough data"."""

    def _plan(self, tools):
        stub = _StubClient(json.dumps({"tools": tools}))
        result = build_planner_node(stub)(
            {
                "query": "IT stocks with PE under 25 and ROE above 15%",
                "objective": "screen",
                "entities": [],
                "complexity": "moderate",
                "intent": "screener",
            }
        )
        return sorted({t["tool"] for t in result["tasks"]})

    def test_two_of_three_become_all_three(self):
        assert self._plan(["screener_parse_query", "screener_run"]) == [
            "screener_parse_query",
            "screener_run",
            "screener_to_markdown",
        ]

    def test_one_tool_pulls_in_the_rest(self):
        assert "screener_to_markdown" in self._plan(["screener_run"])

    def test_other_questions_get_no_screener_tools(self):
        assert self._plan(["fetch_top_movers"]) == ["fetch_top_movers"]
