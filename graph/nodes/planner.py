"""
Planner node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). Splits responsibility
deliberately: the LLM only decides WHICH tools are relevant to the objective
(a selection problem it's good at); dependency wiring between tasks is always
resolved deterministically from ToolSpec.depends_on (graph.tool_catalogue) —
never trusted to the LLM, since a wrong dependency edge silently corrupts
ResearchExecutor's fan-out (Phase 3) in a way that's hard to catch downstream.

complexity == "simple" or no entities short-circuits to an empty task list —
this is the concrete implementation of "avoid unnecessary tool execution".
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from agents.llm_client import LLMClient
from config import GROQ_MODEL_FAST, TOKENS_AGENT
from graph.journal import log_decision
from graph.state import GraphState, ResearchPlan, Task
from graph.tool_catalogue import catalogue_summary, get_tool, list_tools

log = logging.getLogger(__name__)

_SYSTEM = (
    "You select which research tools are needed to answer a financial-research "
    "objective for ARIA. Respond with ONLY a JSON object, no prose, no markdown "
    'fences: {"tools": ["tool_name", ...]}. Pick only tools whose data the '
    "objective actually needs — do not include a tool just because it's "
    "available. If a tool depends on another tool's output, you don't need to "
    "include the dependency yourself; it's added automatically."
)

_ALL_TOOL_NAMES = {spec.name for spec in list_tools()}


def _build_prompt(objective: str, entities: list[str]) -> str:
    return (
        f"Objective: {objective}\n"
        f"Entities: {', '.join(entities)}\n\n"
        f"Available tools:\n{catalogue_summary()}"
    )


def _parse_tool_selection(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text.strip())
        tools = parsed.get("tools", [])
        return [t for t in tools if t in _ALL_TOOL_NAMES]
    except (json.JSONDecodeError, AttributeError, TypeError):
        log.warning("Planner: failed to parse tool selection: %r", raw)
        return []


def _with_transitive_deps(selected: list[str]) -> list[str]:
    """Auto-include any dependency the LLM didn't explicitly select."""
    result: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in get_tool(name).depends_on:
            add(dep)
        result.append(name)

    for name in selected:
        add(name)
    return result


def _build_tasks(tool_names: list[str], entities: list[str]) -> list[Task]:
    tasks: list[Task] = []
    for entity in entities:
        for tool_name in tool_names:
            spec = get_tool(tool_name)
            task: Task = {
                "id": f"{tool_name}:{entity}",
                "description": f"{spec.description} ({entity})",
                "tool": tool_name,
                "entity": entity,
                "depends_on": [f"{dep}:{entity}" for dep in spec.depends_on],
                "status": "pending",
                "result_evidence_ids": [],
            }
            tasks.append(task)
    return tasks


def build_planner_node(client: LLMClient | None = None) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM)

    def planner(state: GraphState) -> dict:
        objective = state.get("objective", "")
        entities = state.get("entities", [])
        complexity = state.get("complexity", "simple")

        if complexity == "simple" or not entities:
            plan: ResearchPlan = {"objective": objective, "tasks": [], "complexity": complexity}
            return {
                "plan": plan,
                "tasks": [],
                **log_decision(
                    "Planner",
                    "skip ResearchExecutor",
                    f"complexity={complexity}, entities={entities or 'none'}",
                ),
            }

        raw = client.ask(
            _build_prompt(objective, entities),
            model=GROQ_MODEL_FAST,
            max_tokens=TOKENS_AGENT,
            temperature=0.0,
        )
        selected = _parse_tool_selection(raw)
        tool_names = _with_transitive_deps(selected)
        tasks = _build_tasks(tool_names, entities)
        plan: ResearchPlan = {"objective": objective, "tasks": tasks, "complexity": complexity}

        return {
            "plan": plan,
            "tasks": tasks,
            **log_decision(
                "Planner",
                f"generated {len(tasks)} tasks across {len(tool_names)} tools",
                f"objective={objective!r}, entities={entities}",
            ),
        }

    return planner
