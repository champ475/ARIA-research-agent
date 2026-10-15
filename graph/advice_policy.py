"""
graph/advice_policy.py — the advice boundary in code (COMP-01; docs/ADVICE_POLICY.md).

ARIA's posture is education-only. This module holds the fixed texts the
guardrails (graph/guardrails.py) attach or answer with, so wording lives in
one reviewed place rather than in prompts the LLM may or may not follow.
"""

from __future__ import annotations

POSTURE = "education_only"

FOOTER_MARKER = "ℹ️ *Educational information, not investment advice."

FOOTERS = {
    "planning": (
        f"{FOOTER_MARKER} This is a model portfolio for your risk band, not a personal "
        "recommendation. ARIA is not a SEBI-registered investment adviser — for decisions about your "
        "own money, consult one. Past performance doesn't predict future returns.*"
    ),
    "report": (
        f"{FOOTER_MARKER} This report summarises public data and is not a research recommendation; "
        "ARIA is not a SEBI-registered research analyst. Verify figures against the source before "
        "relying on them.*"
    ),
    "research": (
        f"{FOOTER_MARKER} ARIA is not SEBI-registered and doesn't tell you what to buy or sell. "
        "Data can be delayed or wrong — check the source before acting.*"
    ),
}

# Requests answered without any research or LLM call.
REFUSALS = {
    "insider": (
        "I can't help with that. Trading on unpublished price-sensitive information is insider "
        "trading under SEBI's Prohibition of Insider Trading Regulations, and I won't speculate on it "
        "either. I'm happy to walk through a company's published results or filings instead."
    ),
    "manipulation": (
        "I can't help with that. Coordinated buying to move a price, pump-and-dump schemes and "
        "\"operator\" stocks are market manipulation under SEBI's PFUTP Regulations. If you've been "
        "pitched one, treat it as a red flag. I can explain how to spot these schemes if that's useful."
    ),
    "guarantee": (
        'No investment can guarantee returns — anything promising "assured", "risk-free" or '
        '"sure-shot" high returns is a warning sign. Higher expected returns always come with '
        "higher risk. If you tell me your horizon and how much volatility you can live with, I can show "
        "what returns different asset mixes have historically delivered, and how much they swung."
    ),
}

# Added to the answer prompt when a request asks for a personal call.
REFRAME_INSTRUCTION = (
    "\n\nADVICE BOUNDARY: the user asked for a personal buy/sell/hold decision or a tip. ARIA is "
    "education-only and not SEBI-registered. Do NOT tell them to buy, sell or hold, do NOT give a "
    "target price as advice, and do NOT rate it a 'strong buy' or similar. Instead lay out what the "
    "data shows, the main arguments on both sides, the key risks, and what they should check or ask "
    "a SEBI-registered adviser before deciding."
)
CAUTION_NOTE = (
    "ℹ️ *Unlisted, penny and SME stocks can be illiquid and hard to exit, and grey-market prices "
    "aren't official. Treat tips about them with extra caution.*"
)
REWRITE_NOTE = (
    "ℹ️ *A sentence that read as a promised return or a personal buy/sell call was removed — "
    "ARIA gives educational information, not advice.*"
)
