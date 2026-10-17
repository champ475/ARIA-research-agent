"""ARCH-05: CommunicationLayer only renders; planning, report management and
file output are their own nodes, wired in the graph."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

from graph.graph import build_graph

ROOT = Path(__file__).resolve().parents[2]


class _Stub:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def ask(self, prompt, **kwargs):
        self.calls += 1
        return self.response


def _graph(intent: str, planner_tools=(), complexity: str = "moderate"):
    clients = {
        "query_analyzer_client": _Stub(
            json.dumps({"intent": intent, "objective": "x", "complexity": complexity})
        ),
        "planner_client": _Stub(json.dumps({"tools": list(planner_tools)})),
        "reflection_client": _Stub(json.dumps({"missing": []})),
        "verification_client": _Stub(json.dumps({"issues": []})),
        "communication_client": _Stub("rendered"),
    }
    return build_graph(**clients), clients


def _journal_nodes(out) -> list[str]:
    return [j["node"] for j in out.get("research_journal", [])]


def test_communication_layer_imports_no_database_or_audit_module():
    tree = ast.parse((ROOT / "graph" / "nodes" / "communication_layer.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not {m for m in imported if m.startswith(("utils.db", "utils.audit", "utils.screener_files"))}


def test_planning_turn_skips_the_research_pipeline():
    graph, clients = _graph("planning")
    with (
        patch("graph.nodes.planning_node.profile_get", return_value={}),
        patch(
            "graph.nodes.planning_node._planning_intake_turn", return_value=({}, False, "Tell me your age.")
        ),
    ):
        out = graph.invoke({"query": "help me plan my investments", "user_id": 1, "max_loops": 2})
    assert out["final_report"] == "Tell me your age."
    assert clients["planner_client"].calls == 0
    assert clients["communication_client"].calls == 0
    assert "PlanningNode" in _journal_nodes(out)


def test_report_listing_skips_planner_and_communication():
    graph, clients = _graph("report")
    with patch("graph.nodes.report_management.report_list", return_value=[]):
        out = graph.invoke({"query": "show my reports", "user_id": 1, "max_loops": 2})
    assert "no saved reports" in out["final_report"]
    assert clients["planner_client"].calls == 0
    assert "ReportManagementNode" in _journal_nodes(out)


def test_report_generation_still_takes_the_normal_path():
    graph, clients = _graph("report")
    graph.invoke({"query": "generate a report on TCS", "user_id": 1, "max_loops": 2})
    assert clients["planner_client"].calls == 1


def test_download_without_an_id_is_not_report_management():
    graph, clients = _graph("report")
    graph.invoke({"query": "download the report", "user_id": 1, "max_loops": 2})
    assert clients["planner_client"].calls == 1


def test_report_management_needs_the_report_intent():
    from graph.nodes.report_management import is_report_management

    assert is_report_management({"intent": "report", "query": "my reports", "user_id": 1})
    assert not is_report_management({"intent": "equity", "query": "my reports", "user_id": 1})
