"""
GraphState schema and reducers for ARIA's research graph.

See RFC_AGENTIC_RESEARCH_GRAPH.md §5 and §7 for the design rationale — this
module is a direct implementation of those two sections. Evidence is the
universal currency: every node downstream of ResearchExecutor consumes
`evidence`/`evidence_summary`/`conclusions`, never a raw tool result.
"""
from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class Evidence(TypedDict):
    id: str                     # hash(source, entity, evidence_type, args)
    source: str                  # "yfinance" | "nse_bhavcopy" | "pdf_scraper" | "news_rss" | "amfi" | "alerts_table"
    tool: str                    # fully-qualified tool function name that produced it
    entity: str                  # ticker / fund code / sector
    evidence_type: str           # "price_ohlcv" | "fundamental_ratio" | "technical_indicator"
                                  # | "news_headline" | "pdf_extract" | "macro" | "risk_flag"
                                  # | "mf_nav" | "mf_category_funds" | "mf_comparison"
                                  # | "alert_list" | "alert_action"
    payload: dict
    timestamp: datetime
    confidence: float            # source reliability x freshness
    citation_key: str            # matches tools/analysis/fundamental.py's cited_footnotes() convention


class Task(TypedDict):
    id: str
    description: str
    tool: str
    entity: str
    depends_on: list[str]
    status: Literal["pending", "running", "done", "failed", "skipped"]
    result_evidence_ids: list[str]


class ResearchPlan(TypedDict):
    objective: str
    tasks: list[Task]
    complexity: Literal["simple", "moderate", "complex"]


class VerificationIssue(TypedDict):
    claim: str
    issue_type: Literal["unsupported", "contradiction", "low_confidence", "missing_citation"]
    stage: Literal["deterministic", "semantic", "llm_judge"]
    evidence_ids: list[str]


class MissingInfoItem(TypedDict):
    # Reflection's output shape — structured, not prose, so Planner can turn
    # each item directly into a Task without a re-interpretation LLM call.
    missing: str
    reason: str
    priority: Literal["high", "medium", "low"]
    action: str


class Conclusion(TypedDict):
    # ConclusionBuilder's output: a decided fact, before any LLM phrases it.
    claim: str
    value: Any
    confidence: float
    evidence_ids: list[str]
    citation_key: str


class JournalEntry(TypedDict):
    node: str
    decision: str
    reason: str
    timestamp: datetime


def append_dedup(existing: list[Evidence], new: list[Evidence]) -> list[Evidence]:
    """
    Append new Evidence, dropping items that duplicate an existing entry's
    (source, entity, evidence_type, citation_key) within that entry's TTL.
    TTL enforcement itself lives in the evidence cache (tools/data/evidence_cache.py,
    RFC §8) — this reducer only prevents the same fetch from being appended
    twice within a single graph run.
    """
    existing = list(existing or [])
    new = list(new or [])

    def key(ev: Evidence) -> tuple:
        return (ev["source"], ev["entity"], ev["evidence_type"], ev["citation_key"])

    seen = {key(ev) for ev in existing}
    for ev in new:
        k = key(ev)
        if k in seen:
            continue
        seen.add(k)
        existing.append(ev)
    return existing


def upsert_by_id(existing: list[Task], new: list[Task]) -> list[Task]:
    """
    Merge Task entries by id. A Task already present keeps its latest status
    unless `new` explicitly updates it — this is what lets Reflection append
    fresh tasks on a loop-back without resetting already-`done` work.
    """
    existing = list(existing or [])
    new = list(new or [])

    by_id = {task["id"]: task for task in existing}
    order = [task["id"] for task in existing]
    for task in new:
        if task["id"] not in by_id:
            order.append(task["id"])
        by_id[task["id"]] = task
    return [by_id[task_id] for task_id in order]


class GraphState(TypedDict):
    messages: Annotated[list, add_messages]
    query: str
    user_id: int              # needed by any domain that persists (reports, screeners, alerts, profiles)
    entities: list[str]
    intent: str
    secondary_intents: list[str]  # Phase 7.4: other domains this message ALSO names, if any
    objective: str
    complexity: str
    plan: ResearchPlan
    tasks: Annotated[list[Task], upsert_by_id]
    evidence: Annotated[list[Evidence], append_dedup]
    evidence_summary: dict            # EvidenceSummarizer output — what Reflection reads
    missing_info: list[MissingInfoItem]
    verification_issues: list[VerificationIssue]
    conclusions: list[Conclusion]     # ConclusionBuilder output — what CommunicationLayer reads
    loop_count: Annotated[int, operator.add]
    max_loops: int
    confidence_summary: dict
    research_journal: Annotated[list[JournalEntry], operator.add]
    final_report: str
    chart_html: str
    pdf_path: str | None       # report-domain file output (Phase 7.1), None for other intents
    md_path: str | None        # report-domain file output (Phase 7.1), None for other intents
    csv_path: str | None       # screener-domain file output (Phase 7.3), None for other intents
