"""
utils/planning_logic.py
Shared intake/strategy-synthesis logic for the financial-planning domain.
Extracted from agents/planning_agent.py (Phase 7.3 of the graph-expansion
plan) so both the legacy PlanningAgent and the LangGraph CommunicationLayer
node run identical intake/strategy logic against the same `financial_profiles`
DB row — see ARCHITECTURE.md's 2026-07-24 "Planning domain on the graph"
entry for why this lives in CommunicationLayer rather than the Task/Evidence
pipeline the rest of the graph uses.
"""

from __future__ import annotations

import json

from config import TOKENS_COMPLEX, TOKENS_FAST
from tools.data.amfi import format_fund_search_results, get_category_funds
from tools.data.fetcher import get_macro
from tools.data.news_fetcher import market_news
from tools.screener import run as screener_run
from tools.screener import to_markdown as screener_md
from utils.financial_profile import REQUIRED_FIELDS as _PROFILE_REQUIRED
from utils.financial_profile import is_complete, rejected_note, validate_updates
from utils.helpers import now_ist

# Minimum fields needed before a first strategy can be generated at all
# (VAL-05: the schema and the build trigger live in utils/financial_profile.py).
REQUIRED_FIELDS = list(_PROFILE_REQUIRED)

# Full field vocabulary shown to the intake LLM so it knows what's worth
# asking about — not a rigid checklist, it decides pacing/phrasing itself.
FIELD_GUIDE = """
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

# Standard fund categories to ground the strategy in real AMFI data —
# refined slightly by risk tolerance/horizon inside fetch_fund_data().
CORE_CATEGORIES = ["large cap", "flexi cap", "mid cap", "small cap", "debt", "elss"]


def fallback_question(profile: dict) -> str:
    missing = [f for f in REQUIRED_FIELDS if profile.get(f) in (None, "")]
    if not missing:
        return "I have enough to build an initial plan — want me to go ahead?"
    labels = {
        "age": "your age",
        "capital": "how much you're investing (lump sum)",
        "horizon_years": "your time horizon in years",
        "goal": "what this money is for",
        "risk_tolerance": "your risk comfort (conservative/moderate/aggressive)",
    }
    asks = ", ".join(labels[f] for f in missing)
    return f"To build a plan tailored to you, tell me {asks}."


def intake_turn(client, msg: str, profile: dict) -> tuple[dict, bool, str]:
    """One combined LLM call: extract any profile fields from `msg`, decide
    whether the minimum viable profile is now complete, and draft a
    conversational reply. `client` is a caller-owned LLMClient (equity_agent/
    report_agent-style dependency injection, not a new provider instance).

    VAL-05: the LLM only proposes `updates`; each field is validated on its
    own (utils/financial_profile.py) and only valid ones are returned for
    saving. Rejected values are asked about in the reply, never stored, and
    completeness is judged on validated values only — the LLM's own
    "minimum_complete" isn't trusted."""
    prompt = _INTAKE_PROMPT.format(
        field_guide=FIELD_GUIDE,
        profile_json=json.dumps(profile) if profile else "{}",
        message=msg,
    )
    try:
        raw = client.ask(prompt, temperature=0.4, max_tokens=TOKENS_FAST)
        from utils.helpers import strip_code_fence

        raw = strip_code_fence(raw)
        data = json.loads(raw)
        proposed = data.get("updates") or {}
        if not isinstance(proposed, dict):
            proposed = {}
        clean, rejected = validate_updates(proposed)
        merged = {**profile, **clean}
        minimum_complete = is_complete(merged)
        reply = (data.get("reply") if not rejected else None) or fallback_question(merged)
        return clean, minimum_complete, reply + rejected_note(rejected, proposed)
    except Exception:
        return {}, is_complete(profile), fallback_question(profile)


def fetch_fund_data(profile: dict) -> str:
    categories = list(CORE_CATEGORIES)
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


def fetch_equity_screen() -> str:
    try:
        results = screener_run({"min_roe": 15, "max_de": 1.5, "min_roce": 12}, max_stocks=40)
        return screener_md(results[:15], {"min_roe": 15, "max_de": 1.5, "min_roce": 12})
    except Exception as e:
        return f"Equity quality screen unavailable: {e}"


def fetch_macro_news() -> str:
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


def sanity_note(profile: dict) -> str:
    """Soft, informational reference point — never an enforced clamp on the
    LLM-designed allocation."""
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


def build_strategy_prompt(profile: dict) -> str:
    fund_data = fetch_fund_data(profile)
    equity_screen = fetch_equity_screen()
    from graph.prompt_safety import wrap_untrusted

    # Headlines are third-party text: fenced as data (SEC-03).
    macro_news = wrap_untrusted("news", fetch_macro_news())

    return f"""Build a complete, personalised long-term investment strategy for this investor.

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
(Lump sum vs phased/STP-in vs SIP — how to actually put the capital {profile.get("capital", "N/A")} to work
given the horizon of {profile.get("horizon_years", "N/A")} years; a 30-year horizon with a large lump sum
usually warrants phased deployment over 6-12 months, not investing it all on day one)

### Glide Path / Rebalancing Checkpoints
(How the allocation should shift as the goal approaches — give 2-3 concrete
checkpoints across the horizon, not just day-1 numbers)

### Gaps to Address First
(Insurance, emergency fund, high-interest debt — only if relevant to this profile)

### Risks & Assumptions
*This is educational analysis, not SEBI-registered investment advice.*
"""


def build_strategy(client, profile: dict) -> str:
    out = client.chat(build_strategy_prompt(profile), temperature=0.4, max_tokens=TOKENS_COMPLEX)
    return out + sanity_note(profile)
