"""tools/analysis/sentiment.py — VADER sentiment with keyword fallback"""

import re
from collections import Counter
from langchain_core.tools import tool

# Try VADER (vaderSentiment package); fall back to keyword matching if not installed
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderAnalyzer
    _vader = _VaderAnalyzer()
    VADER_OK = True
except ImportError:
    _vader = None
    VADER_OK = False

# Keyword fallback lists (used when VADER is unavailable)
BULL = [
    "surge","soar","rally","jump","gain","rise","climb","boost","record","high",
    "profit","growth","expansion","acquisition","upgrade","buy","outperform",
    "positive","beat","exceed","launch","win","contract","order","partnership",
    "deal","investment","funding","ipo","dividend","buyback","strong","robust",
    "recovery","turnaround","optimistic","bullish","beat expectations","guidance raised",
]
BEAR = [
    "fall","drop","decline","crash","loss","miss","weak","downgrade","sell",
    "underperform","negative","concern","risk","debt","default","fraud","probe",
    "sebi","fine","penalty","slowdown","cut","reduce","layoff","exit","bearish",
    "warning","caution","volatile","disappoint","below","short","guidance cut",
    "margin pressure","debt default","audit qualification","pledging",
]
MODIFIERS = ["may","could","might","expected","likely","possible","reportedly"]

# Finance-domain adjustments layered on top of VADER scores
_FINANCE_BOOST = {
    "beat": 0.15, "upgrade": 0.15, "dividend": 0.10, "buyback": 0.10,
    "outperform": 0.15, "guidance raised": 0.20, "record profit": 0.20,
    "strong results": 0.15, "acquisition": 0.10, "order win": 0.15,
}
_FINANCE_DRAG = {
    "downgrade": -0.15, "fraud": -0.25, "probe": -0.20, "default": -0.25,
    "penalty": -0.15, "pledging": -0.15, "guidance cut": -0.20,
    "margin pressure": -0.15, "audit qualification": -0.20, "layoff": -0.10,
}


@tool(description="Score a piece of financial news text for sentiment in the range [-1.0, +1.0].")
def score_text(text: str) -> float:
    """
    Score a piece of text in [-1.0, +1.0].
    Uses VADER compound score when available, with finance-domain adjustments.
    Falls back to keyword matching if vaderSentiment is not installed.
    """
    if VADER_OK:
        compound = _vader.polarity_scores(text)["compound"]  # already in [-1, 1]
        # Apply finance-domain boosts/drags
        lo = text.lower()
        adjustment = 0.0
        for phrase, delta in _FINANCE_BOOST.items():
            if phrase in lo:
                adjustment += delta
        for phrase, delta in _FINANCE_DRAG.items():
            if phrase in lo:
                adjustment += delta
        raw = compound + adjustment
        return round(max(-1.0, min(1.0, raw)), 3)

    # Keyword fallback
    lo    = text.lower()
    bull  = sum(1 for w in BULL if re.search(r'\b' + re.escape(w) + r'\b', lo))
    bear  = sum(1 for w in BEAR if re.search(r'\b' + re.escape(w) + r'\b', lo))
    mod   = sum(1 for m in MODIFIERS if m in lo)
    total = bull + bear
    if total == 0:
        return 0.0
    raw   = (bull - bear) / total
    damp  = max(0.6, 1 - mod * 0.08)
    return round(raw * damp, 3)


@tool(description="Convert a numeric sentiment score into a human-readable bullish/bearish label.")
def label(score: float) -> str:
    if score >= 0.35:  return "🟢 Bullish"
    if score >= 0.10:  return "🟡 Slightly Bullish"
    if score >= -0.10: return "⚪ Neutral"
    if score >= -0.35: return "🟠 Slightly Bearish"
    return "🔴 Bearish"


@tool(description="Analyse a list of news item dicts and return overall sentiment score, label, and per-item breakdown.")
def analyse(news_items: list[dict]) -> dict:
    if not news_items:
        return {"score": 0.0, "label": "⚪ Neutral", "items": [], "bull_kws": [], "bear_kws": [], "text": "No news available."}

    scored = []
    bull_c = Counter()
    bear_c = Counter()

    for item in news_items:
        text = f"{item.get('title','')} {item.get('summary','')}"
        s    = score_text(text)
        lo   = text.lower()
        # Keyword tracking for explainability (regardless of scoring method)
        bk   = [w for w in BULL if re.search(r'\b' + re.escape(w) + r'\b', lo)]
        brk  = [w for w in BEAR if re.search(r'\b' + re.escape(w) + r'\b', lo)]
        bull_c.update(bk)
        bear_c.update(brk)
        scored.append({"title": item.get("title",""), "score": s, "label": label(s)})

    overall = round(sum(x["score"] for x in scored) / len(scored), 3)
    lbl     = label(overall)
    engine  = "VADER + finance adjustments" if VADER_OK else "keyword matching (install vaderSentiment for better accuracy)"

    text_lines = [
        f"**Overall Sentiment:** {lbl} (score: {overall:+.3f}) — *{engine}*",
        f"**Bullish signals:** {', '.join(f'{k}({n})' for k,n in bull_c.most_common(5)) or 'None'}",
        f"**Bearish signals:** {', '.join(f'{k}({n})' for k,n in bear_c.most_common(5)) or 'None'}",
        "",
        "**News breakdown:**",
    ] + [f"- {x['label']} | {x['title'][:90]}" for x in scored[:8]]

    return {
        "score"   : overall,
        "label"   : lbl,
        "items"   : scored,
        "bull_kws": bull_c.most_common(5),
        "bear_kws": bear_c.most_common(5),
        "text"    : "\n".join(text_lines),
    }
