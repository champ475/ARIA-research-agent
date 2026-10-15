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
import logging

from config import TOKENS_COMPLEX, TOKENS_FAST
from tools.data.amfi import format_fund_search_results, get_category_funds
from tools.data.fetcher import get_macro
from tools.data.news_fetcher import market_news
from utils.financial_profile import REQUIRED_FIELDS as _PROFILE_REQUIRED
from utils.financial_profile import is_complete, rejected_note, validate_updates
from utils.helpers import now_ist

log = logging.getLogger(__name__)

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


def _horizon(profile: dict) -> float | None:
    try:
        return float(profile.get("horizon_years"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fetch_fund_data(profile: dict) -> str:
    categories = list(CORE_CATEGORIES)
    tax_regime = str(profile.get("tax_regime", "")).lower()
    if "new" in tax_regime:
        categories = [c for c in categories if c != "elss"]  # ELSS 80C benefit is old-regime only

    blocks = []
    for cat in categories:
        try:
            # DATA-04: ranked by the CAGR that matches the investor's horizon.
            funds = get_category_funds(cat, top_n=5, horizon_years=_horizon(profile))
            blocks.append(f"--- {cat.title()} (AMFI NAV data) ---\n{format_fund_search_results(funds)}")
        except Exception as e:
            log.warning("[planning] fund data for %s unavailable: %s", cat, e)
            blocks.append(f"--- {cat.title()} ---\nUnavailable (data source error).")
    return "\n\n".join(blocks)


def fetch_macro_news() -> str:
    try:
        macro = get_macro()
        macro_txt = "\n".join(
            f"- {k}: {v.get('price')} ({'+' if isinstance(v.get('change'), float) and v['change'] >= 0 else ''}{v.get('change')}%)"
            for k, v in macro.items()
        )
    except Exception as e:
        log.warning("[planning] macro data unavailable: %s", e)
        macro_txt = "Unavailable (data source error)."
    try:
        news = market_news()
        news_txt = "\n".join(f"- {n['title']}" for n in news[:6])
    except Exception as e:
        log.warning("[planning] market news unavailable: %s", e)
        news_txt = "Unavailable (data source error)."
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
    """COMP-01 (docs/ADVICE_POLICY.md): education-only. The plan is a MODEL
    PORTFOLIO for the investor's risk band, adapted to their horizon —
    asset classes and fund categories, with named funds only as examples of
    a category, never "buy this fund/stock". No direct-equity picks for a
    person. The disclaimer footer is appended in code (graph/guardrails.py),
    not requested from the model."""
    fund_data = fetch_fund_data(profile)
    from graph.prompt_safety import wrap_untrusted

    # Headlines are third-party text: fenced as data (SEC-03).
    macro_news = wrap_untrusted("news", fetch_macro_news())
    band = profile.get("risk_tolerance", "moderate")

    return f"""Write an EDUCATIONAL model-portfolio explanation for this investor's risk band.
ARIA is not a SEBI-registered adviser: describe what a typical {band} portfolio for this horizon
looks like and why, so they can make their own decision. Never tell them to buy a specific fund or
stock; never promise returns.

INVESTOR PROFILE:
{json.dumps(profile, indent=2)}

=== RESEARCH DATA (fund names may only appear as EXAMPLES of a category, taken from this data) ===

--- Mutual Fund Categories and Example Funds (AMFI) ---
{fund_data}

--- Macro & Market Context ---
{macro_news}

=== INSTRUCTIONS ===
## Model Portfolio — {band.title()} risk band — {now_ist()}

### Profile Summary
(Restate their situation in 2-3 lines; note anything concerning — e.g. missing insurance or
emergency fund, a risk tolerance that doesn't fit their age or horizon — before anything else)

### Model Allocation for a {band} investor
(The typical equity/debt/gold/cash split for this risk band, as ranges, and how a horizon of
{profile.get("horizon_years", "N/A")} years shifts it. Explain the reasoning for each asset class.)

### Fund Categories in Each Sleeve
(For each sleeve, the SEBI fund categories that fit it — e.g. large cap / flexi cap / index for core
equity, short-duration or corporate-bond funds for debt — what each category does, and what to
compare when choosing within it: expense ratio, 5-year returns versus the category, consistency,
fund size. You may name 1-2 funds per category from the data above, explicitly as examples.)

### Deployment Approach
(Lump sum vs phased/STP vs SIP for capital of {profile.get("capital", "N/A")} — how investors in this
band commonly phase in a large amount, and why)

### Rebalancing and Glide Path
(2-3 checkpoints across the horizon showing how the mix typically becomes more conservative as the
goal approaches)

### Gaps to Address First
(Insurance, emergency fund, high-interest debt — only if relevant to this profile)

### Risks and What to Ask an Adviser
(The main risks of this model portfolio, and the questions worth taking to a SEBI-registered adviser)
"""


def build_strategy(client, profile: dict) -> str:
    """LLM-03: rupee amounts and return/performance figures in the plan must
    trace to the prompt's data (the allocation percentages are the model
    portfolio's design, not data claims). One regeneration, then a note."""
    from graph.grounding import REGENERATE_INSTRUCTION, check, record, unverified_note

    prompt = build_strategy_prompt(profile)
    out = client.chat(prompt, temperature=0.4, max_tokens=TOKENS_COMPLEX)
    result = check(out, prompt, mode="planning")
    if not result.ok:
        out = client.ask(
            prompt + REGENERATE_INSTRUCTION.format(figures=", ".join(result.unsupported)),
            temperature=0.2,
            max_tokens=TOKENS_COMPLEX,
        )
        result = check(out, prompt, mode="planning")
        result.regenerated = True
        if not result.ok:
            out += unverified_note(result.unsupported)
    record(result)
    return out + sanity_note(profile)
