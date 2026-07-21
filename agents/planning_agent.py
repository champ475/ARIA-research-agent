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
"""

import json

from agents.llm_client import LLMClient
from config import TOKENS_COMPLEX, TOKENS_FAST
from tools.data.fetcher import get_macro
from tools.data.news_fetcher import market_news
from tools.data.amfi import get_category_funds, format_fund_search_results
from tools.screener import run as screener_run, to_markdown as screener_md
from utils.helpers import now_ist
from utils.db import profile_get, profile_upsert, profile_clear

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

# Minimum fields needed before a first strategy can be generated at all.
_REQUIRED = ["age", "capital", "horizon_years", "goal", "risk_tolerance"]

# Full field vocabulary shown to the intake LLM so it knows what's worth
# asking about — not a rigid checklist, it decides pacing/phrasing itself.
_FIELD_GUIDE = """
MINIMUM (must have before any plan can be built):
- age (years)
- capital (lump sum available now, in INR)
- horizon_years (investment horizon in years — convert "long term"/"30 years" etc to a number)
- goal (retirement, house, child's education, wealth-building, no fixed goal, ...)
- risk_tolerance (self-rated: conservative/moderate/aggressive — flag if it seems mismatched with age/horizon)

NICE-TO-HAVE (refine the plan, ask opportunistically, don't block on these):
- monthly_sip (ongoing monthly investment capacity, if any)
- dependents (spouse, kids, aging parents — affects emergency fund & insurance sizing)
- income_stability (salaried / business / freelance)
- existing_investments (what they already hold, to avoid duplicate exposure)
- existing_debts (loans/EMIs — affects how much should go to debt paydown vs investing)
- tax_regime (old/new — affects ELSS/80C relevance)
- insurance_cover (term life + health — flag if missing, this matters before equity investing)
- emergency_fund (do they have 6+ months expenses set aside already)
- liquidity_needs (any known withdrawal needed before the horizon ends)
- behavioral_notes (e.g. panic-sold in a past crash — affects how aggressive to actually recommend)
"""

_INTAKE_PROMPT = """{field_guide}

CURRENT KNOWN PROFILE (JSON, may be empty):
{profile_json}

USER'S LATEST MESSAGE: "{message}"

Task: (1) extract any fields above you can confidently fill in from this
message, (2) decide if the MINIMUM fields are now all known, (3) write a
natural, conversational reply — NOT a robotic numbered form. If minimum
fields are missing, ask for 1-3 of the missing ones that flow naturally from
what the user just said (prioritize MINIMUM over nice-to-have, don't ask for
fields already known). If minimum fields are now complete, say so and offer
to build the plan now while inviting any nice-to-have detail they want to add
first.

Return ONLY valid JSON, no markdown fences:
{{"updates": {{...fields extracted this turn...}}, "minimum_complete": true or false, "reply": "..."}}
"""

_BUILD_TRIGGER_KW = [
    "build my plan", "build the plan", "generate my plan", "generate the plan",
    "create my plan", "go ahead", "build it", "yes build", "generate strategy",
    "build my strategy", "create my strategy", "ready", "sounds good", "let's go",
    "proceed", "make the plan",
]

# Standard fund categories to ground the strategy in real AMFI data —
# refined slightly by risk tolerance/horizon inside _fetch_fund_data().
_CORE_CATEGORIES = ["large cap", "flexi cap", "mid cap", "small cap", "debt", "elss"]


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
        updates, minimum_complete, reply = self._intake_turn(msg, profile)
        if updates:
            profile = profile_upsert(self._user_id, updates)

        was_already_complete = all(profile.get(f) not in (None, "") for f in _REQUIRED) and not updates
        wants_build = any(k in lower for k in _BUILD_TRIGGER_KW)

        if not minimum_complete:
            return reply, None

        # Build immediately the turn minimum info completes, or whenever the
        # user explicitly asks — no extra gating once we have enough to work with.
        if updates or wants_build or not was_already_complete:
            return self._build_strategy(profile), None
        return reply, None

    # ── Intake ───────────────────────────────────────────────────────────────

    def _intake_turn(self, msg: str, profile: dict) -> tuple[dict, bool, str]:
        prompt = _INTAKE_PROMPT.format(
            field_guide=_FIELD_GUIDE,
            profile_json=json.dumps(profile) if profile else "{}",
            message=msg,
        )
        try:
            raw = self._client.ask(prompt, temperature=0.4, max_tokens=TOKENS_FAST)
            raw = raw.strip().strip("```json").strip("```").strip()
            data = json.loads(raw)
            updates = data.get("updates") or {}
            if not isinstance(updates, dict):
                updates = {}
            merged = {**profile, **updates}
            minimum_complete = all(merged.get(f) not in (None, "") for f in _REQUIRED)
            reply = data.get("reply") or self._fallback_question(merged)
            return updates, minimum_complete, reply
        except Exception:
            minimum_complete = all(profile.get(f) not in (None, "") for f in _REQUIRED)
            return {}, minimum_complete, self._fallback_question(profile)

    def _fallback_question(self, profile: dict) -> str:
        missing = [f for f in _REQUIRED if profile.get(f) in (None, "")]
        if not missing:
            return "I have enough to build an initial plan — want me to go ahead?"
        labels = {
            "age": "your age", "capital": "how much you're investing (lump sum)",
            "horizon_years": "your time horizon in years", "goal": "what this money is for",
            "risk_tolerance": "your risk comfort (conservative/moderate/aggressive)",
        }
        asks = ", ".join(labels[f] for f in missing)
        return f"To build a plan tailored to you, tell me {asks}."

    def _show_profile(self) -> str:
        profile = profile_get(self._user_id)
        if not profile:
            return "I don't have a profile for you yet. Tell me your age, investable capital, time horizon, and goal to get started."
        lines = ["## Your Financial Profile\n", "| Field | Value |", "|---|---|"]
        for k, v in profile.items():
            lines.append(f"| {k.replace('_',' ').title()} | {v} |")
        return "\n".join(lines)

    # ── Research bundle ──────────────────────────────────────────────────────

    def _fetch_fund_data(self, profile: dict) -> str:
        categories = list(_CORE_CATEGORIES)
        tax_regime = str(profile.get("tax_regime", "")).lower()
        if "new" in tax_regime:
            categories = [c for c in categories if c != "elss"]  # ELSS 80C benefit is old-regime only

        blocks = []
        for cat in categories:
            try:
                funds = get_category_funds(cat, top_n=5)
                blocks.append(f"--- {cat.title()} (AMFI NAV data) ---\n{format_fund_search_results(funds)}")
            except Exception as e:
                blocks.append(f"--- {cat.title()} ---\nUnavailable: {e}")
        return "\n\n".join(blocks)

    def _fetch_equity_screen(self) -> str:
        try:
            results = screener_run({"min_roe": 15, "max_de": 1.5, "min_roce": 12}, max_stocks=40)
            return screener_md(results[:15], {"min_roe": 15, "max_de": 1.5, "min_roce": 12})
        except Exception as e:
            return f"Equity quality screen unavailable: {e}"

    def _fetch_macro_news(self) -> str:
        try:
            macro = get_macro()
            macro_txt = "\n".join(
                f"- {k}: {v.get('price')} ({'+' if isinstance(v.get('change'), float) and v['change'] >= 0 else ''}{v.get('change')}%)"
                for k, v in macro.items()
            )
        except Exception as e:
            macro_txt = f"Unavailable: {e}"
        try:
            news = market_news()
            news_txt = "\n".join(f"- {n['title']}" for n in news[:6])
        except Exception as e:
            news_txt = f"Unavailable: {e}"
        return f"MACRO:\n{macro_txt}\n\nHEADLINES:\n{news_txt}"

    # ── Sanity note (soft, informational — never enforced) ─────────────────────

    def _sanity_note(self, profile: dict) -> str:
        try:
            age = float(profile.get("age"))
        except (TypeError, ValueError):
            return ""
        horizon_bonus = 0
        try:
            horizon = float(profile.get("horizon_years"))
            if horizon >= 15:
                horizon_bonus = 10
        except (TypeError, ValueError):
            pass
        baseline = max(20, min(90, int(100 - age + horizon_bonus)))
        return (
            f"\n\n---\n*📐 Rule-of-thumb reference point only (not a constraint on the plan above): "
            f"for age {int(age)} with this horizon, a commonly cited baseline equity allocation is "
            f"roughly {baseline}%. If the plan above lands far outside that, it should be for a "
            f"reason stated above — ask me to explain or adjust if it doesn't make sense to you.*"
        )

    # ── Strategy synthesis ───────────────────────────────────────────────────

    def _build_strategy(self, profile: dict) -> str:
        fund_data = self._fetch_fund_data(profile)
        equity_screen = self._fetch_equity_screen()
        macro_news = self._fetch_macro_news()

        prompt = f"""Build a complete, personalised long-term investment strategy for this investor.

INVESTOR PROFILE:
{json.dumps(profile, indent=2)}

=== RESEARCH DATA (ground every fund/stock name and macro claim in this — never invent one) ===

--- Mutual Fund Options (AMFI) ---
{fund_data}

--- Quality-Factor Equity Screen (ROE>15, D/E<1.5, ROCE>12 — candidates for direct equity sleeve) ---
{equity_screen}

--- Macro & Market Context ---
{macro_news}

=== INSTRUCTIONS ===
Design the allocation yourself based on this specific person — don't apply a
generic template. Cover:

## Investment Strategy — {now_ist()}

### Profile Summary
(Restate their situation in 2-3 lines, note anything concerning — e.g. missing
insurance/emergency fund, risk tolerance mismatched with age/horizon — before the plan)

### Asset Allocation
(Your recommended equity/debt/gold/international/cash split with % — justify
each number against THIS profile, not a rule of thumb)

### Equity Sleeve
(Direct stocks and/or equity mutual funds — pick from the data above, explain why)

### Debt & Stability Sleeve
(Funds from the data above appropriate for their horizon and risk tolerance)

### Deployment Plan
(Lump sum vs phased/STP-in vs SIP — how to actually put the capital {profile.get('capital', 'N/A')} to work
given the horizon of {profile.get('horizon_years', 'N/A')} years; a 30-year horizon with a large lump sum
usually warrants phased deployment over 6-12 months, not investing it all on day one)

### Glide Path / Rebalancing Checkpoints
(How the allocation should shift as the goal approaches — give 2-3 concrete
checkpoints across the horizon, not just day-1 numbers)

### Gaps to Address First
(Insurance, emergency fund, high-interest debt — only if relevant to this profile)

### Risks & Assumptions
*This is educational analysis, not SEBI-registered investment advice.*
"""
        out = self._client.chat(prompt, temperature=0.4, max_tokens=TOKENS_COMPLEX)
        return out + self._sanity_note(profile)

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
