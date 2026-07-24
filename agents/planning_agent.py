"""
agents/planning_agent.py
Financial Planner Agent — goal-based, multi-asset investment strategy.

Two-phase conversation, both phases LLM-driven (not a rigid form):
1. INTAKE — one combined LLM call per turn both extracts any profile fields
   it can confidently pull from the user's message AND decides what to ask
   next, conversationally. Profile persists in Postgres (financial_profiles)
   so it survives across sessions instead of being re-asked every time.
2. STRATEGY — once the minimum viable profile is known, a second LLM call
   gets the full profile plus a real research bundle (macro, live fund data
   from AMFI, a quality-factor stock screen from the sector universe, news)
   and freely designs the allocation itself — no hardcoded allocation
   formula. A soft, informational sanity-check note (not an enforced clamp)
   is appended so the user has a reference point without the LLM being
   boxed in.

Intake/strategy logic itself lives in utils/planning_logic.py (Phase 7.3 of
the graph-expansion plan) so graph/nodes/communication_layer.py's planning
branch runs identical logic against the same financial_profiles DB row —
this class is now a thin wrapper supplying its own LLMClient and the
CRUD/dispatch shell around that shared logic.
"""

from agents.llm_client import LLMClient
from utils.db import profile_get, profile_upsert, profile_clear
from utils.planning_logic import (
    REQUIRED_FIELDS as _REQUIRED,
    BUILD_TRIGGER_KW as _BUILD_TRIGGER_KW,
    intake_turn as _shared_intake_turn,
    build_strategy as _shared_build_strategy,
)

SYSTEM = """
You are ARIA Financial Planner — a goal-based, multi-asset investment
strategist for Indian investors (equity, debt, gold, mutual funds), reasoning
like a senior fee-only financial planner, not a stock-tips bot.

You build strategies tailored to the specific person in front of you — their
age, capital, time horizon, goal, dependents, risk appetite, existing
investments/debts, insurance cover, and liquidity needs all change the right
answer. Never give a generic "60/40 for everyone" answer.

Ground every fund/stock you name in the real data provided to you — never
invent a fund name, NAV, or stock that isn't in the data bundle. Cite macro
numbers when relevant (rate cycle, valuation regime) instead of vague claims.

This is educational analysis, not SEBI-registered investment advice — say so
once, briefly, don't repeat it every message.
"""


class PlanningAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._user_id = user_id

    @property
    def provider(self):
        return self._client.provider

    def process(self, message: str) -> tuple[str, None]:
        msg = message.strip()
        lower = msg.lower()

        if any(k in lower for k in ["show my profile", "my financial profile", "what do you know about me"]):
            return self._show_profile(), None
        if any(k in lower for k in ["start over", "reset my plan", "reset my profile", "forget my profile", "clear my profile"]):
            profile_clear(self._user_id)
            return "🗑️ Profile cleared. Tell me about yourself again whenever you're ready — age, capital, time horizon, and goal are the essentials.", None

        profile = profile_get(self._user_id)
        updates, minimum_complete, reply = _shared_intake_turn(self._client, msg, profile)
        if updates:
            profile = profile_upsert(self._user_id, updates)

        was_already_complete = all(profile.get(f) not in (None, "") for f in _REQUIRED) and not updates
        wants_build = any(k in lower for k in _BUILD_TRIGGER_KW)

        if not minimum_complete:
            return reply, None

        # Build immediately the turn minimum info completes, or whenever the
        # user explicitly asks — no extra gating once we have enough to work with.
        if updates or wants_build or not was_already_complete:
            return _shared_build_strategy(self._client, profile), None
        return reply, None

    def _show_profile(self) -> str:
        profile = profile_get(self._user_id)
        if not profile:
            return "I don't have a profile for you yet. Tell me your age, investable capital, time horizon, and goal to get started."
        lines = ["## Your Financial Profile\n", "| Field | Value |", "|---|---|"]
        for k, v in profile.items():
            lines.append(f"| {k.replace('_',' ').title()} | {v} |")
        return "\n".join(lines)

    # ── Standard agent contract ─────────────────────────────────────────────

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Show my financial profile",
            "Adjust my risk tolerance",
            "Explain the equity sleeve picks",
            "Update my monthly SIP amount",
        ]

    def reset(self):
        self._client.reset()

    def load_history(self, messages: list[dict]):
        self._client.load_history(messages)
