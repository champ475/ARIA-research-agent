"""
graph/data_questions.py — data questions never take the no-research path (LLM-06).

QueryAnalyzer's cheap classifier sometimes labels "what's TCS trading at?"
as "simple", and the simple path answers from the model's training data —
stale and possibly invented — with no warning. `min_complexity` overrides
that deterministically: a resolved company, a market-wide data phrase, or a
data word in a non-definitional question makes the turn at least
"moderate", so tools run. Definitions ("what is RSI?") stay simple.

`NO_LIVE_DATA_INSTRUCTION` is added to every simple-path answer prompt.
"""

from __future__ import annotations

import re

_I = re.IGNORECASE

DATA_WORDS = re.compile(
    r"\b(?:price|prices|trading\s+at|quote|ltp|cmp|p\s*/?\s*e|pe|p\s*/?\s*b|pb|nav|rsi|macd|returns?|cagr|"
    r"market\s*cap|mcap|52[\s-]*week|dividends?|yield|eps|roe|roce|revenue|profits?|earnings|results|"
    r"valuation|volume|share\s+price|stock\s+price|doing|perform(?:ance|ing)?|up|down|high|low|"
    r"cheap|expensive|undervalued|overvalued|outlook|chart|technicals?|fundamentals?|moved?|fell|rose)\b",
    _I,
)
MARKET_DATA = re.compile(
    r"\b(?:gainers|losers|movers|top\s+stocks|nifty|sensex|bank\s*nifty|"
    r"market\b[^.?!]{0,20}\b(?:today|now|update|this\s+week)|"
    r"bulk\s+deals?|block\s+deals?|best\s+[\w\s-]{0,20}funds?|fund\s+returns?|nav\s+of)\b",
    _I,
)
DEFINITIONAL = re.compile(
    r"^\s*(?:what\s+(?:is|are|does)|whats|what's|explain|define|meaning\s+of|how\s+(?:does|do)\s+\w+\s+work|"
    r"tell\s+me\s+(?:what|about)\s+(?:a|an|the)?\s*(?:p/?e|rsi|macd|nav|sip|elss|cagr|ratio|indicator))",
    _I,
)

NO_LIVE_DATA_INSTRUCTION = (
    "You have no live market data in this answer. Do not state any current or recent price, ratio, "
    "return, NAV or index level, and don't estimate one from memory. If the user needs a figure, offer "
    "to look it up (e.g. 'ask me for TCS's current P/E and I'll fetch it').\n\n"
)

_ORDER = {"simple": 0, "moderate": 1, "complex": 2}


def min_complexity(query: str, entities: list[str], llm_complexity: str) -> tuple[str, str | None]:
    """(complexity, reason it was raised or None)."""
    current = llm_complexity if llm_complexity in _ORDER else "simple"
    if current != "simple":
        return current, None
    if entities:
        return "moderate", f"names {', '.join(entities)}"
    if MARKET_DATA.search(query or ""):
        return "moderate", "asks for market data"
    if DATA_WORDS.search(query or "") and not DEFINITIONAL.search(query or ""):
        return "moderate", "asks for a figure"
    return current, None
