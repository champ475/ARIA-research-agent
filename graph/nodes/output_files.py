"""
OutputFilesNode (ARCH-05): saves the finished answer as a report and stores a
screener's rows as a CSV, after CommunicationLayer has written the text.

`final_report` does not exist until CommunicationLayer's LLM call completes, so
this has to run after it; but saving to the database is not rendering, so it is
a node of its own (it used to be inside CommunicationLayer). OPS-02: nothing is
rendered to disk: the report is saved to `reports.content` and the answer
carries /download/report/<id>.pdf|.md links that any replica serves by
re-rendering the stored text (web/download_routes.py); the CSV is stored for a
day in `downloads`. A storage failure never blocks the answer itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from graph.guardrails import finalize
from graph.journal import log_decision
from graph.nodes.results import report_urls
from graph.state import GraphState
from utils.audit import audit
from utils.db import download_put, report_save
from utils.helpers import detect_report_type
from utils.screener_files import screener_csv_bytes

log = logging.getLogger(__name__)

NODE = "OutputFiles"


def _intents(state: GraphState) -> set[str]:
    """The primary intent plus QueryAnalyzer's secondary ones (multi-intent turns)."""
    return {state.get("intent")} | set(state.get("secondary_intents") or [])


def _find_screener_results(evidence: Sequence[Mapping[str, Any]]) -> list[dict] | None:
    for ev in evidence:
        if ev.get("evidence_type") == "screener_results" and isinstance(ev.get("payload"), list):
            return ev["payload"]
    return None


def wants_files(state: GraphState) -> bool:
    """A synthesised answer for a report or screener turn (not a clarification,
    a refusal, a streamed chat answer or a "no evidence" reply)."""
    intents = _intents(state)
    return bool(
        state.get("conclusions")
        and state.get("final_report")
        and not state.get("pending_synthesis_prompt")
        and intents & {"report", "screener"}
    )


def output_files(state: GraphState) -> dict:
    intents = _intents(state)
    entities = state.get("entities", [])
    evidence = state.get("evidence", [])
    primary_entity = entities[0] if entities else None
    report = state.get("final_report", "")
    urls: dict[str, str | None] = {"pdf_url": None, "md_url": None, "csv_url": None}
    user_id = state.get("user_id")

    if "report" in intents and primary_entity:
        query = state.get("query", "")
        rtype = detect_report_type((query or state.get("objective", "")).lower())
        # LLM-09: the saved/downloaded report carries the same checks and
        # footer as the chat answer (finalize() is idempotent).
        report = finalize(report, dict(state))
        if user_id is not None:
            try:
                report_id = report_save(user_id, primary_entity, rtype, f"{rtype} — {primary_entity}", report)
                urls.update(report_urls(report_id))
                audit(
                    "report.generate", actor_user_id=user_id, target=primary_entity, details={"type": rtype}
                )
            except Exception as e:
                # Persistence failure shouldn't block returning the report itself.
                log.warning(f"[OutputFiles] report_save failed: {e}")

    if "screener" in intents and user_id is not None:
        screener_results = _find_screener_results(evidence)
        if screener_results:
            try:
                token = download_put(
                    user_id,
                    "aria_screener_results.csv",
                    "text/csv",
                    screener_csv_bytes(screener_results),
                )
                urls["csv_url"] = f"/download/file/{token}"
            except Exception as e:
                log.warning("[OutputFiles] screener CSV not stored: %s", type(e).__name__)

    return {
        "final_report": report,
        **urls,
        **log_decision(
            NODE,
            "stored output files",
            (f"report={urls['pdf_url']}" if "report" in intents else "")
            + (f", csv={'yes' if urls['csv_url'] else 'no'}" if "screener" in intents else ""),
        ),
    }
