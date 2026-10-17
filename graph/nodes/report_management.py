"""
ReportManagementNode (ARCH-05): "show my reports" and "download report #N".

These are a database read and a re-render, so there is no tool-selection or
evidence-gathering step for Planner or ResearchExecutor to do. They used to be
handled inside CommunicationLayer, which meant the node that is supposed to
only render conclusions also read the database. The router after QueryAnalyzer
(`is_report_management`) now sends them here, and the node ends the turn.
"""

from __future__ import annotations

import re

from graph.nodes.results import empty_result, report_urls
from graph.state import GraphState
from utils.db import report_get, report_list
from utils.escaping import md_cell

NODE = "ReportManagementNode"

_REPORT_LIST_KW = [
    "list reports",
    "list my reports",
    "saved reports",
    "my reports",
    "show reports",
    "show my reports",
]
_REPORT_DOWNLOAD_KW = ["download", "regenerate", "resend", "re-download", "redownload"]
# VAL-08: only "report #12" / "report 12" names an id — "report for 2024" doesn't.
_REPORT_ID_RE = re.compile(r"\breport\s*(?:#|no\.?|number)?\s*(\d+)\b", re.IGNORECASE)


def is_report_management(state: GraphState) -> bool:
    """True for a report-list request, or a download request that names a report.
    A plain "generate a report on TCS" (or "download" with no id) is not one: it
    goes through the normal research flow."""
    if state.get("intent") != "report":
        return False
    query = (state.get("query") or "").strip()
    lower = query.lower()
    if any(k in lower for k in _REPORT_LIST_KW):
        return True
    return (
        any(k in lower for k in _REPORT_DOWNLOAD_KW)
        and state.get("user_id") is not None
        and bool(_REPORT_ID_RE.search(query))
    )


def report_management(state: GraphState) -> dict:
    user_id = state.get("user_id")
    query = (state.get("query") or "").strip()
    lower = query.lower()

    if any(k in lower for k in _REPORT_LIST_KW):
        if user_id is None:
            return empty_result(
                "I need to know who you are to show your reports — please sign in again.",
                NODE,
                "report: missing user_id",
                "cannot read reports without a user_id",
            )
        reports = report_list(user_id)
        if not reports:
            text = 'You have no saved reports yet. Try "generate an IC report for TCS".'
        else:
            lines = ["## Your Reports\n", "| # | Type | Title | Date |", "|---|------|-------|------|"]
            for r in reports:
                lines.append(
                    f"| {r['id']} | {md_cell(r['report_type'])} | {md_cell(r['title'])} | "
                    f"{r['created_at'].strftime('%Y-%m-%d')} |"
                )
            lines.append('\nSay "download report #<id>" to get the PDF/Markdown again.')
            text = "\n".join(lines)
        return empty_result(text, NODE, "report: listed saved reports", f"user_id={user_id}")

    id_match = _REPORT_ID_RE.search(query)
    if user_id is None or not id_match:  # not reachable through the router; stay safe if called directly
        return empty_result(
            'Tell me which report: say "show my reports" for the list, then "download report #<id>".',
            NODE,
            "report: no target",
            f"user_id={user_id}",
        )
    report_id = int(id_match.group(1))
    row = report_get(user_id, report_id)
    if row is None:
        return empty_result(
            f'Couldn\'t find report #{report_id} — try "show my reports" to see the list.',
            NODE,
            "report: download target not found",
            f"user_id={user_id}, report_id={report_id}",
        )
    # OPS-02: the link re-renders from reports.content on download.
    result = empty_result(
        f"Here's report #{report_id} — **{row['title']}**.",
        NODE,
        "report: regenerated from history",
        f"user_id={user_id}, report_id={report_id}",
    )
    result.update(report_urls(report_id))
    return result
