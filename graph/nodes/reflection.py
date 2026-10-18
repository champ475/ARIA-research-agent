"""
Reflection node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). Reads evidence_summary
(never the raw evidence list — that's EvidenceSummarizer's whole point) and
emits structured MissingInfoItem entries instead of a prose sentence, so
Planner-style task generation can consume `action` directly without another
LLM call to interpret free text.

Task generation for a missing item reuses Planner's tool-selection/expansion
helpers (graph.nodes.planner) rather than duplicating the dependency-
resolution logic — same reasoning as Planner itself: dependency wiring is
never trusted to the LLM.

loop_count only increments when Reflection actually requests more work; if
the loop budget is already exhausted, gaps are still reported (so
CommunicationLayer/ConclusionBuilder can disclose them) but no new tasks are
queued, since Planner/ResearchExecutor won't be revisited anyway.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import prompts
from agents.llm_client import LLMClient, LLMProvidersExhausted
from config import EVIDENCE_PROMPT_CHAR_BUDGET, REFLECTION_SKIP_WHEN_COMPLETE, TOKENS_AGENT
from graph.deadline import past_research_cutoff
from graph.journal import log_decision
from graph.nodes.evidence_summarizer import cap_evidence_for_prompt
from graph.nodes.planner import _ALL_TOOL_NAMES, _build_tasks, _with_transitive_deps
from graph.prompt_safety import wrap_untrusted
from graph.schemas import ReflectionResult
from graph.state import GraphState, MissingInfoItem, Task
from graph.write_policy import WRITE_TOOLS
from utils.llm_json import ParseFailure, extract_json, json_reply

log = logging.getLogger(__name__)

_SYSTEM = prompts.get("reflection.system") + prompts.get("safety.untrusted_notice")

# SEC-03: Reflection fills research gaps; it may never add a tool that writes.
_REFLECTION_TOOL_NAMES = _ALL_TOOL_NAMES - WRITE_TOOLS

_VALID_PRIORITIES = {"high", "medium", "low"}
_DEFAULT_PRIORITY = "medium"


def _done_tools(tasks: list[dict]) -> str:
    """ "fetch_ohlcv(TCS), ..." for every finished task, so the model does not ask for
    data that was already collected (live trace: "weekly price data" after fetch_ohlcv)."""
    done = [f"{t.get('tool')}({t.get('entity')})" for t in tasks if t.get("status") == "done"]
    return ", ".join(sorted(set(done)))


def _build_prompt(query: str, objective: str, evidence_summary: dict, tasks: list[dict] | None = None) -> str:
    capped = cap_evidence_for_prompt(evidence_summary, EVIDENCE_PROMPT_CHAR_BUDGET)
    ran = _done_tools(tasks or [])
    return (
        f"Original query:\n{wrap_untrusted('user', query)}\n"
        f"Objective: {objective}\n\n"
        f"Evidence collected so far:\n{wrap_untrusted('evidence', json.dumps(capped, default=str))}\n\n"
        + (f"Tools already run (do not ask for these again): {ran}\n" if ran else "")
        + f"Available tools: {', '.join(sorted(_REFLECTION_TOOL_NAMES))}"
    )


def _parse(raw: str) -> list[MissingInfoItem]:
    try:
        parsed = extract_json(raw)
        raw_items = parsed.get("missing", [])
    except (ParseFailure, AttributeError, TypeError):
        log.warning("Reflection: failed to parse LLM output as JSON: %r", raw)
        return []

    items: list[MissingInfoItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict) or "missing" not in entry or "action" not in entry:
            continue
        priority = entry.get("priority", _DEFAULT_PRIORITY)
        if priority not in _VALID_PRIORITIES:
            priority = _DEFAULT_PRIORITY
        items.append(
            {
                "missing": entry["missing"],
                "reason": entry.get("reason", ""),
                "priority": priority,
                "action": entry["action"],
            }
        )
    return items


def _plan_fully_succeeded(state: GraphState) -> bool:
    tasks = state.get("tasks") or []
    return (
        state.get("loop_count", 0) == 0
        and state.get("complexity") in ("simple", "moderate")
        and bool(tasks)
        and all(t.get("status") == "done" for t in tasks)
    )


def build_reflection_node(
    client: LLMClient | None = None,
    provider_config: dict | None = None,
) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM, provider_config, node="reflection")

    def reflection(state: GraphState) -> dict:
        loop_count = state.get("loop_count", 0)
        max_loops = state.get("max_loops", 2)
        if past_research_cutoff(state):
            return log_decision("Reflection", "skipped", "turn deadline reached — no further research")
        if REFLECTION_SKIP_WHEN_COMPLETE and _plan_fully_succeeded(state):
            # LLM-11: a moderate question whose every planned tool returned
            # data has no gap for an extra model call to find.
            return {
                "missing_info": [],
                **log_decision("Reflection", "skipped", "every planned task succeeded (moderate question)"),
            }

        try:
            raw = json_reply(
                client,
                ReflectionResult,
                "reflection",
                _build_prompt(
                    state.get("query", ""),
                    state.get("objective") or state.get("query", ""),
                    state.get("evidence_summary", {}),
                    state.get("tasks", []),
                ),
                max_tokens=TOKENS_AGENT,
                temperature=0.0,
            )
        except LLMProvidersExhausted as e:
            # "Couldn't check" must never be conflated with "checked, found
            # nothing" — forcing loop_count to max_loops (loop_count's
            # reducer is Annotated[int, operator.add], see graph/state.py)
            # makes _route_after_verification's loop_count < max_loops
            # check exit terminally instead of looping on a doomed retry.
            log.warning(f"Reflection: all LLM providers exhausted, treating as terminal — {e}")
            return {
                "missing_info": [],
                "loop_count": max_loops,
                **log_decision("Reflection", "skipped — all LLM providers exhausted", type(e).__name__),
            }
        missing_items = _parse(raw)

        if not missing_items:
            return {
                "missing_info": missing_items,
                **log_decision("Reflection", "proceed to Verification", "no gaps found, objective covered"),
            }

        if loop_count >= max_loops:
            return {
                "missing_info": missing_items,
                **log_decision(
                    "Reflection",
                    "proceed to Verification despite unresolved gaps",
                    f"loop budget exhausted ({loop_count}/{max_loops})",
                ),
            }

        valid_actions = [item["action"] for item in missing_items if item["action"] in _REFLECTION_TOOL_NAMES]
        tool_names = _with_transitive_deps(valid_actions)
        candidate_tasks = _build_tasks(tool_names, state.get("entities", []))

        existing_tasks = {t["id"]: t for t in state.get("tasks", [])}
        new_tasks: list[Task] = [
            t for t in candidate_tasks if existing_tasks.get(t["id"], {}).get("status") != "done"
        ]

        if not new_tasks:
            # Every gap names a tool that already ran: no new work, so no loop to spend.
            return {
                "missing_info": missing_items,
                **log_decision(
                    "Reflection",
                    "proceed to Verification, gaps already covered by finished tasks",
                    f"{len(missing_items)} gap(s): {[m['missing'] for m in missing_items]}",
                ),
            }

        return {
            "missing_info": missing_items,
            "tasks": new_tasks,
            "loop_count": 1,
            **log_decision(
                "Reflection",
                f"requesting {len(new_tasks)} more task(s)",
                f"{len(missing_items)} gap(s): {[m['missing'] for m in missing_items]}",
            ),
        }

    return reflection
