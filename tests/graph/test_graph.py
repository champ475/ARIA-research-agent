import json

from graph.graph import build_graph


class _StubClient:
    def __init__(self, response: str):
        self._response = response

    def ask(self, prompt, **kwargs):
        return self._response


def _build(qa_response: dict, planner_response: dict = None):
    qa = _StubClient(json.dumps(qa_response))
    planner = _StubClient(json.dumps(planner_response or {}))
    return build_graph(query_analyzer_client=qa, planner_client=planner)


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
