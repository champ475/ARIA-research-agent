"""Shared result shapes for the nodes that end a turn without the research
pipeline (PlanningNode, ReportManagementNode) and for the report links."""

from __future__ import annotations

from graph.journal import log_decision


def report_urls(report_id: int) -> dict[str, str]:
    """OPS-02: download links any replica can serve (web/download_routes.py)."""
    return {"pdf_url": f"/download/report/{report_id}.pdf", "md_url": f"/download/report/{report_id}.md"}


def empty_result(final_report: str, node: str, decision: str, reason: str) -> dict:
    """A finished answer with no chart, files or pending synthesis."""
    return {
        "final_report": final_report,
        "pending_synthesis_prompt": None,
        "pending_fundamental_payload": None,
        "chart_html": None,
        "pdf_url": None,
        "md_url": None,
        "csv_url": None,
        "confidence_summary": {},
        **log_decision(node, decision, reason),
    }
