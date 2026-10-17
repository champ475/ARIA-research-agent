"""
PlanningNode (ARCH-05): the financial-planning intake and strategy flow.

Planning bypasses the Evidence/Conclusion pipeline: it is two sequential LLM
calls (utils/planning_logic.py) with Python-only branching between them (is the
minimum profile complete? did the user ask to build?), reading and writing
`financial_profiles` directly. Nothing in graph/tool_catalogue.py models
"extract structured profile fields from free text", so there is no
tool-selection or evidence step for Planner or ResearchExecutor to do. The
router after QueryAnalyzer sends `intent == "planning"` here and the node ends
the turn. It used to live in CommunicationLayer, the node meant to only render
conclusions (ARCHITECTURE.md: 2026-07-24 "Planning domain on the graph", and
the ARCH-05 entry; the third HITL gate, capital-allocation confirmation, is
still deliberately absent).
"""

from __future__ import annotations

from collections.abc import Callable

import prompts
from agents.llm_client import LLMClient
from graph.nodes.results import empty_result
from graph.state import GraphState
from utils.audit import audit
from utils.db import profile_clear, profile_get, profile_upsert
from utils.financial_profile import is_complete as _profile_complete
from utils.financial_profile import profile_summary as _profile_summary
from utils.financial_profile import sanitize_profile as _sanitize_profile
from utils.financial_profile import wants_build as _wants_build
from utils.planning_logic import (
    build_strategy as _planning_build_strategy,
)
from utils.planning_logic import (
    intake_turn as _planning_intake_turn,
)

NODE = "PlanningNode"

_SYSTEM = prompts.get("communication.system") + prompts.get("safety.untrusted_notice")

_PLANNING_SHOW_KW = ["show my profile", "my financial profile", "what do you know about me"]
_PLANNING_RESET_KW = [
    "start over",
    "reset my plan",
    "reset my profile",
    "forget my profile",
    "clear my profile",
]


def _handle_planning(client: LLMClient, state: GraphState) -> dict:
    user_id = state.get("user_id")
    query = (state.get("query") or "").strip()
    lower = query.lower()

    if user_id is None:
        return empty_result(
            "I need to know who you are to track a financial profile — please sign in again.",
            NODE,
            "planning: missing user_id",
            "cannot read/write financial_profiles without a user_id",
        )

    if any(k in lower for k in _PLANNING_SHOW_KW):
        profile = profile_get(user_id)
        if not profile:
            text = "I don't have a profile for you yet. Tell me your age, investable capital, time horizon, and goal to get started."
        else:
            lines = ["## Your Financial Profile\n", "| Field | Value |", "|---|---|"]
            for k, v in profile.items():
                lines.append(f"| {k.replace('_', ' ').title()} | {v} |")
            text = "\n".join(lines)
        return empty_result(text, NODE, "planning: showed profile", f"user_id={user_id}")

    if any(k in lower for k in _PLANNING_RESET_KW):
        profile_clear(user_id)
        return empty_result(
            "🗑️ Profile cleared. Tell me about yourself again whenever you're ready — age, capital, time horizon, and goal are the essentials.",
            NODE,
            "planning: cleared profile",
            f"user_id={user_id}",
        )

    # VAL-05: stored rows from before validation may hold invalid values —
    # never plan on them; they're simply asked for again.
    profile = _sanitize_profile(profile_get(user_id))
    updates, minimum_complete, reply = _planning_intake_turn(client, query, profile)
    if updates:
        profile = _sanitize_profile(profile_upsert(user_id, updates))
    minimum_complete = _profile_complete(profile)

    if not minimum_complete:
        return empty_result(
            reply, NODE, "planning: intake incomplete", f"user_id={user_id}, updates={list(updates)}"
        )

    if _wants_build(query):
        strategy = _planning_build_strategy(client, profile)
        audit("plan.generate", actor_user_id=user_id, details={"fields": sorted(profile)})
        return empty_result(strategy, NODE, "planning: built strategy", f"user_id={user_id}")

    # Complete but not asked for: show exactly what was saved first (the
    # capital-allocation check), and build only on an explicit request.
    text = (
        f"{reply}\n\n**Here's what I've saved — please check it:**\n\n{_profile_summary(profile)}\n\n"
        "When this looks right, say **build my plan** (or correct anything above)."
    )
    return empty_result(
        text, NODE, "planning: profile complete, awaiting explicit build", f"user_id={user_id}"
    )


def build_planning_node(
    client: LLMClient | None = None, provider_config: dict | None = None
) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM, provider_config, node="communication")

    def planning(state: GraphState) -> dict:
        return _handle_planning(client, state)

    return planning
