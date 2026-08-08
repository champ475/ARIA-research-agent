"""
Typed registry wrapping the existing tools/data and tools/analysis functions
for use by Planner/ResearchExecutor (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). None
of the wrapped functions are rewritten — this module only adapts their
heterogeneous signatures (some take a bare ticker, some take another tool's
raw output) to one uniform shape: fn(entity, context) -> raw_result, where
`context` maps a dependency tool's name to its already-fetched raw result.

Scope for Phase 2 was deliberately narrow (single-ticker equity data only,
per the migration plan). Phase 7 brings each remaining intent onto the graph
one domain at a time — report_agent's tools are the first (Phase 7.1);
mf and alert follow in 7.2 (this module); screener/planning follow in 7.3.

Phase 7.2 known gaps (documented rather than papered over — see
ARCHITECTURE.md's 2026-07-24 "alert domain" entry for the full writeup):
- `alert_create` IS registered here (added when the legacy keyword-routed
  orchestrator/agents were retired — see ARCHITECTURE.md's LangGraph-as-only-
  path entry). It does NOT need a Task-schema `args` field: like
  `screener_parse_query` reading `context["_query"]` for free text, it
  extracts `alert_type`/`threshold`/`condition` straight out of the same
  raw-message context every task already receives — a regex pre-filter for
  the two unambiguous plain-price cases, falling back to a cheap LLM
  extraction call otherwise (ported from the deleted
  `agents/alert_agent.py::_regex_extract`/`_llm_extract`).
- `alert_list`/`alert_cancel`/`alert_pause`/`alert_resume`/`alert_create` need the calling
  user's `user_id`, which lives on GraphState, not in a dependency's raw
  result — `graph/nodes/research_executor.py::_build_context` injects it as
  `context["_user_id"]` for every task (not just alert ones) so these tools
  can read it the same way they'd read a real dependency's output.
- Entity-less alert queries (e.g. "show my alerts", with no ticker
  mentioned) won't generate a Task at all under Planner's current
  `complexity=="simple" or not entities` short-circuit (graph/nodes/planner.py) —
  that gate assumes every domain needs an entity to research, which isn't
  true for alert-list/mf-news/planning-intake style queries. Not fixed here;
  flagged for Phase 7.3/7.4 alongside the args-field work above.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from tools.data.fetcher import get_info, get_financials, get_ohlcv
from tools.data.news_fetcher import company_news, google_news
from tools.data.pdf_scraper import get_latest_quarterly_results, get_annual_report_text
from tools.data.amfi import get_all_nav, search_fund, get_category_funds, get_mf_comparison
from tools.data.nse_scraper import get_top_movers, get_bulk_deals, get_block_deals
from tools.screener import parse_query as screener_parse_query, run as run_screener, to_markdown as screener_to_markdown
from tools.analysis.fundamental import compute as compute_fundamental_ratios
from tools.analysis.technical import compute_all as compute_technical_indicators
from tools.analysis.technical import snapshot as technical_snapshot
from utils.db import (
    alert_add as db_alert_add,
    alert_list as db_alert_list,
    alert_remove as db_alert_remove,
    alert_pause as db_alert_pause,
    alert_resume as db_alert_resume,
    screener_save as db_screener_save,
)
from utils.helpers import parse_screener_name

# Synthetic entity Planner falls back to when a query has no ticker to
# extract but still needs a Task (whole-market screens, alert-list, mf
# category/news) — see graph/nodes/planner.py's module docstring for the
# short-circuit change this enables. Defined here, not in planner.py, since
# it's really a tool-catalogue-level convention: these are the tools that
# know how to interpret "no specific entity" and ignore `entity` accordingly.
NO_ENTITY_SENTINEL = "market"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    evidence_type: str
    depends_on: tuple[str, ...]
    cost: Literal["low", "medium", "high"]
    fn: Callable[[str, dict[str, Any]], Any]
    description: str


def _fetch_info(entity: str, context: dict[str, Any]) -> dict:
    return get_info(entity)


def _fetch_financials(entity: str, context: dict[str, Any]) -> dict:
    return get_financials(entity)


def _fetch_ohlcv(entity: str, context: dict[str, Any]) -> Any:
    return get_ohlcv(entity)


def _fetch_news(entity: str, context: dict[str, Any]) -> list[dict]:
    return company_news(entity)


def _compute_fundamentals(entity: str, context: dict[str, Any]) -> dict:
    return compute_fundamental_ratios(context["fetch_info"], context["fetch_financials"])


def _compute_technicals(entity: str, context: dict[str, Any]) -> dict:
    df = compute_technical_indicators(context["fetch_ohlcv"])
    return technical_snapshot(df)


def _fetch_quarterly_results(entity: str, context: dict[str, Any]) -> str:
    return get_latest_quarterly_results(entity)


def _fetch_annual_report_text(entity: str, context: dict[str, Any]) -> str:
    return get_annual_report_text(entity)


def _fetch_nav(entity: str, context: dict[str, Any]) -> Any:
    """`entity` here is a fund-name/search-string, not a stock ticker —
    mirrors agents/mf_agent.py::_nav_query/_search, which both resolve a
    free-text fund name against the full AMFI NAV table."""
    return search_fund(entity, get_all_nav())


def _fetch_category_funds(entity: str, context: dict[str, Any]) -> Any:
    """`entity` is a category keyword (e.g. "large cap", "elss") — see
    agents/mf_agent.py::_CATEGORY_KW for the vocabulary this expects."""
    return get_category_funds(entity, top_n=10)


def _fetch_mf_comparison(entity: str, context: dict[str, Any]) -> str:
    """`entity` may be a single fund name or 2-4 names joined by " vs "/","
    (mirrors agents/mf_agent.py::_compare's candidate extraction)."""
    import re as _re
    names = [n.strip() for n in _re.split(r"\s+vs\.?\s+|,", entity) if n.strip()]
    if len(names) < 2:
        names = [entity]
    return get_mf_comparison(names[:4])


def _fetch_mf_news(entity: str, context: dict[str, Any]) -> list[dict]:
    return google_news(f"India {entity} mutual fund", n=5)


# ── alert_create extraction (ported from the deleted agents/alert_agent.py) ──
# Structured params (alert_type/threshold/condition) come out of the same
# raw-message context every task already receives (context["_query"]) rather
# than a Task-schema `args` field — see this module's docstring.

_ALERT_TYPES = [
    "price_above", "price_below", "pct_change",
    "rsi_cross_above", "rsi_cross_below",
    "macd_bullish_cross", "macd_bearish_cross",
    "ma_cross", "bb_breakout", "volume_spike",
    "week52_high", "week52_low", "bulk_deal", "portfolio_pnl",
]

_RECURRING_KW = [
    "recurring", "every time", "each time", "repeatedly", "keep alerting",
    "keep notifying", "every day", "daily alert", "don't deactivate", "dont deactivate",
]

_PRICE_ABOVE_RE = re.compile(r"\b(above|over|exceeds?|crosses?\s+above|goes?\s+above)\b", re.I)
_PRICE_BELOW_RE = re.compile(r"\b(below|under|drops?\s+below|falls?\s+below)\b", re.I)
_NUM_RE = re.compile(r"[\d,]+\.?\d*")
_INDICATOR_HINT_KW = [
    "rsi", "macd", "bollinger", "bb ", "moving average", "sma", "ema",
    "volume", "52 week", "52-week", "bulk deal", "block deal",
    "portfolio", "p&l", "pnl", "% change", "percent change", "golden cross",
    "death cross",
]

_EXTRACT_PROMPT = """Extract the alert condition from this user message as JSON only, no prose, no markdown fences.
Message: "{message}"
Symbol already identified: {symbol}

Return exactly this JSON shape:
{{"alert_type": one of {types}, "threshold": number or null, "condition": "short human-readable summary"}}

Rules:
- price_above/price_below/pct_change/rsi_cross_above/rsi_cross_below/volume_spike need a numeric threshold.
- macd_bullish_cross/macd_bearish_cross/ma_cross/bb_breakout/week52_high/week52_low/bulk_deal/portfolio_pnl usually don't need one — use null.
- If you truly cannot determine a valid alert_type from the list, use null for alert_type.
"""

_ALERT_CREATE_SYSTEM = (
    "You extract structured alert conditions from a user's free-text request "
    "for ARIA's alerting system. Respond with ONLY the requested JSON."
)

def _get_extract_client(llm_config: dict | None):
    """Built fresh per call (not a cached singleton) since `llm_config`
    varies per user/turn — a BYO-key user's alert-condition extraction must
    use THEIR provider, not a module-wide default that would silently spend
    the app's trial quota on their behalf."""
    from agents.llm_client import LLMClient
    return LLMClient(system_prompt=_ALERT_CREATE_SYSTEM, provider_config=llm_config)


def _regex_extract_alert(msg: str):
    """Cheap deterministic pre-filter for the two unambiguous plain-price
    cases — avoids an LLM call for the most common alert type. Bails out
    (forces the LLM path) if the message mentions any indicator/pattern
    vocabulary, since "RSI crosses below 30" contains "below" but is NOT
    a plain price_below alert."""
    lower = msg.lower()
    if any(kw in lower for kw in _INDICATOR_HINT_KW):
        return None, None, None
    nums = _NUM_RE.findall(msg.replace(",", ""))
    if not nums:
        return None, None, None
    threshold = float(nums[-1])
    if _PRICE_ABOVE_RE.search(msg):
        return "price_above", threshold, f"Price crosses above ₹{threshold:,.2f}"
    if _PRICE_BELOW_RE.search(msg):
        return "price_below", threshold, f"Price drops below ₹{threshold:,.2f}"
    return None, None, None


def _llm_extract_alert(msg: str, symbol: str, llm_config: dict | None):
    from config import TOKENS_FAST
    prompt = _EXTRACT_PROMPT.format(message=msg, symbol=symbol, types=_ALERT_TYPES)
    try:
        raw = _get_extract_client(llm_config).ask(prompt, temperature=0.0, max_tokens=TOKENS_FAST)
        raw = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw)
        alert_type = data.get("alert_type")
        threshold = data.get("threshold")
        condition = data.get("condition") or (alert_type or "condition met")
        if alert_type not in _ALERT_TYPES:
            return None, None, None
        return alert_type, (float(threshold) if threshold is not None else None), condition
    except Exception:
        return None, None, None


def _alert_create(entity: str, context: dict[str, Any]) -> dict:
    """`entity` is the ticker Planner already resolved (query_analyzer's
    entity extraction runs before this task is ever built) — this tool only
    extracts the CONDITION (alert_type/threshold), not the symbol."""
    msg = context.get("_query") or ""
    user_id = context.get("_user_id")
    symbol = entity

    alert_type, threshold, condition = _regex_extract_alert(msg)
    if alert_type is None:
        alert_type, threshold, condition = _llm_extract_alert(msg, symbol, context.get("_llm_config"))

    if alert_type is None or alert_type not in _ALERT_TYPES:
        return {"action": "create", "symbol": symbol, "result": "unrecognized_condition"}

    recurring = any(kw in msg.lower() for kw in _RECURRING_KW)
    alert_id = db_alert_add(user_id, symbol, alert_type, condition, threshold, recurring=recurring)
    return {
        "action": "create", "symbol": symbol, "result": "created",
        "alert_id": alert_id, "condition": condition, "recurring": recurring,
    }


def _alert_list(entity: str, context: dict[str, Any]) -> list[dict]:
    """`entity` is unused (ignored) — this tool lists ALL of the calling
    user's alerts, not a per-ticker view. `_user_id` is injected into every
    task's context by research_executor.py (see module docstring)."""
    return db_alert_list(context.get("_user_id"), active_only=False)


def _match_alerts_by_symbol(entity: str, context: dict[str, Any]) -> list[dict]:
    alerts = context.get("alert_list") or db_alert_list(context.get("_user_id"), active_only=False)
    return [a for a in alerts if a.get("symbol") == entity]


def _alert_cancel(entity: str, context: dict[str, Any]) -> dict:
    matches = _match_alerts_by_symbol(entity, context)
    user_id = context.get("_user_id")
    if len(matches) != 1:
        return {"action": "cancel", "symbol": entity, "result": "not_found" if not matches else "ambiguous"}
    db_alert_remove(user_id, matches[0]["id"])
    return {"action": "cancel", "symbol": entity, "result": "removed", "alert_id": matches[0]["id"]}


def _alert_pause(entity: str, context: dict[str, Any]) -> dict:
    matches = [a for a in _match_alerts_by_symbol(entity, context) if a.get("active")]
    user_id = context.get("_user_id")
    if len(matches) != 1:
        return {"action": "pause", "symbol": entity, "result": "not_found" if not matches else "ambiguous"}
    db_alert_pause(user_id, matches[0]["id"])
    return {"action": "pause", "symbol": entity, "result": "paused", "alert_id": matches[0]["id"]}


def _alert_resume(entity: str, context: dict[str, Any]) -> dict:
    matches = [a for a in _match_alerts_by_symbol(entity, context) if not a.get("active")]
    user_id = context.get("_user_id")
    if len(matches) != 1:
        return {"action": "resume", "symbol": entity, "result": "not_found" if not matches else "ambiguous"}
    db_alert_resume(user_id, matches[0]["id"])
    return {"action": "resume", "symbol": entity, "result": "resumed", "alert_id": matches[0]["id"]}


def _screener_parse_query(entity: str, context: dict[str, Any]) -> dict:
    """Ignores `entity` (typically the NO_ENTITY_SENTINEL "market") — reads
    the user's raw message from `context["_query"]`, injected by
    research_executor.py, since parse_query() needs free text, not a ticker."""
    return screener_parse_query(context.get("_query") or "")


def _screener_run(entity: str, context: dict[str, Any]) -> list[dict]:
    filters = context.get("screener_parse_query") or {}
    return run_screener(filters)


def _screener_to_markdown(entity: str, context: dict[str, Any]) -> str:
    results = context.get("screener_run") or []
    filters = context.get("screener_parse_query") or {}
    return screener_to_markdown(results, filters)


def _screener_save(entity: str, context: dict[str, Any]) -> dict:
    name = parse_screener_name(context.get("_query") or "")
    filters = context.get("screener_parse_query") or {}
    db_screener_save(context.get("_user_id"), name, filters)
    return {"name": name, "filters": filters}


def _fetch_top_movers(entity: str, context: dict[str, Any]) -> Any:
    query = (context.get("_query") or "").lower()
    return get_top_movers("losers" if "loser" in query else "gainers")


def _fetch_bulk_deals(entity: str, context: dict[str, Any]) -> Any:
    symbol = entity if entity and entity != NO_ENTITY_SENTINEL else None
    return get_bulk_deals(symbol)


def _fetch_block_deals(entity: str, context: dict[str, Any]) -> Any:
    symbol = entity if entity and entity != NO_ENTITY_SENTINEL else None
    return get_block_deals(symbol)


_CATALOGUE: dict[str, ToolSpec] = {}


def _register(spec: ToolSpec) -> None:
    _CATALOGUE[spec.name] = spec


_register(ToolSpec(
    name="fetch_info",
    evidence_type="quote_info",
    depends_on=(),
    cost="low",
    fn=_fetch_info,
    description="Fetch current quote/company-info snapshot for a ticker.",
))
_register(ToolSpec(
    name="fetch_financials",
    evidence_type="raw_financials",
    depends_on=(),
    cost="low",
    fn=_fetch_financials,
    description="Fetch raw balance sheet / income statement / cash flow for a ticker.",
))
_register(ToolSpec(
    name="fetch_ohlcv",
    evidence_type="price_ohlcv",
    depends_on=(),
    cost="low",
    fn=_fetch_ohlcv,
    description="Fetch daily OHLCV price history for a ticker.",
))
_register(ToolSpec(
    name="fetch_news",
    evidence_type="news_headline",
    depends_on=(),
    cost="low",
    fn=_fetch_news,
    description="Fetch recent company news headlines for a ticker.",
))
_register(ToolSpec(
    name="compute_fundamentals",
    evidence_type="fundamental_ratio",
    depends_on=("fetch_info", "fetch_financials"),
    cost="low",
    fn=_compute_fundamentals,
    description="Recompute PE/PB/ROE/ROCE/D-E etc. from raw financials (never trusts upstream ratios).",
))
_register(ToolSpec(
    name="compute_technicals",
    evidence_type="technical_indicator",
    depends_on=("fetch_ohlcv",),
    cost="low",
    fn=_compute_technicals,
    description="Compute RSI/MACD/Bollinger Bands/trend snapshot from OHLCV history.",
))
_register(ToolSpec(
    name="fetch_quarterly_results",
    evidence_type="pdf_extract",
    depends_on=(),
    cost="medium",
    fn=_fetch_quarterly_results,
    description="Fetch latest quarterly results text (NSE/BSE filing PDF), for report-domain queries.",
))
_register(ToolSpec(
    name="fetch_annual_report_text",
    evidence_type="pdf_extract",
    depends_on=(),
    cost="high",
    fn=_fetch_annual_report_text,
    description="Fetch annual report text (PDF parsing, slow), for initiating-coverage-style report queries.",
))
_register(ToolSpec(
    name="fetch_nav",
    evidence_type="mf_nav",
    depends_on=(),
    cost="medium",
    fn=_fetch_nav,
    description="Search AMFI NAV data for mutual funds matching a fund-name search string.",
))
_register(ToolSpec(
    name="fetch_category_funds",
    evidence_type="mf_category_funds",
    depends_on=(),
    cost="low",
    fn=_fetch_category_funds,
    description="Fetch top funds (by AMFI NAV) in a mutual fund category, e.g. 'large cap', 'elss'.",
))
_register(ToolSpec(
    name="fetch_mf_comparison",
    evidence_type="mf_comparison",
    depends_on=(),
    cost="medium",
    fn=_fetch_mf_comparison,
    description="Compare 2-4 named mutual funds side by side.",
))
_register(ToolSpec(
    name="fetch_mf_news",
    evidence_type="news_headline",
    depends_on=(),
    cost="low",
    fn=_fetch_mf_news,
    description="Fetch recent mutual fund industry news for a category or topic.",
))
_register(ToolSpec(
    name="alert_create",
    evidence_type="alert_action",
    depends_on=(),
    cost="low",
    fn=_alert_create,
    description="Create a new price/technical-indicator alert on a ticker from the user's natural-language request (e.g. 'alert me if TCS drops below 3500').",
))
_register(ToolSpec(
    name="alert_list",
    evidence_type="alert_list",
    depends_on=(),
    cost="low",
    fn=_alert_list,
    description="List all of the calling user's alerts (active, paused, and triggered).",
))
_register(ToolSpec(
    name="alert_cancel",
    evidence_type="alert_action",
    depends_on=("alert_list",),
    cost="low",
    fn=_alert_cancel,
    description="Cancel/remove the calling user's alert on a given ticker.",
))
_register(ToolSpec(
    name="alert_pause",
    evidence_type="alert_action",
    depends_on=("alert_list",),
    cost="low",
    fn=_alert_pause,
    description="Pause the calling user's active alert on a given ticker.",
))
_register(ToolSpec(
    name="alert_resume",
    evidence_type="alert_action",
    depends_on=("alert_list",),
    cost="low",
    fn=_alert_resume,
    description="Resume the calling user's paused alert on a given ticker.",
))
_register(ToolSpec(
    name="screener_parse_query",
    evidence_type="screener_filters",
    depends_on=(),
    cost="low",
    fn=_screener_parse_query,
    description="Parse filter criteria (PE/ROE/sector/etc.) from a natural-language screening request.",
))
_register(ToolSpec(
    name="screener_run",
    evidence_type="screener_results",
    depends_on=("screener_parse_query",),
    cost="medium",
    fn=_screener_run,
    description="Run a stock screen across the market universe using parsed filter criteria.",
))
_register(ToolSpec(
    name="screener_to_markdown",
    evidence_type="screener_table",
    depends_on=("screener_run",),
    cost="low",
    fn=_screener_to_markdown,
    description="Render the screen's matching stocks as a Markdown table (use alongside screener_run for a readable result).",
))
_register(ToolSpec(
    name="screener_save",
    evidence_type="screener_saved",
    depends_on=("screener_parse_query",),
    cost="low",
    fn=_screener_save,
    description="Save the current screen's filters under a name for reuse.",
))
_register(ToolSpec(
    name="fetch_top_movers",
    evidence_type="top_movers",
    depends_on=(),
    cost="low",
    fn=_fetch_top_movers,
    description="Fetch today's top gaining or losing stocks (NSE).",
))
_register(ToolSpec(
    name="fetch_bulk_deals",
    evidence_type="bulk_deals",
    depends_on=(),
    cost="low",
    fn=_fetch_bulk_deals,
    description="Fetch bulk deal records, market-wide or for a specific ticker.",
))
_register(ToolSpec(
    name="fetch_block_deals",
    evidence_type="block_deals",
    depends_on=(),
    cost="low",
    fn=_fetch_block_deals,
    description="Fetch block deal records, market-wide or for a specific ticker.",
))


def get_tool(name: str) -> ToolSpec:
    return _CATALOGUE[name]


def list_tools() -> list[ToolSpec]:
    return list(_CATALOGUE.values())


def catalogue_summary() -> str:
    """Compact text form for inclusion in the Planner's prompt."""
    lines = []
    for spec in _CATALOGUE.values():
        deps = f" (needs: {', '.join(spec.depends_on)})" if spec.depends_on else ""
        lines.append(f"- {spec.name}: {spec.description}{deps}")
    return "\n".join(lines)
