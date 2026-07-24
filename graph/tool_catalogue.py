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
- `alert_create` is NOT registered here. Unlike every other tool in this
  catalogue, creating an alert needs structured parameters an LLM must
  extract from free text (alert_type/threshold/condition — see
  agents/alert_agent.py's `_regex_extract`/`_llm_extract`), and Task/ToolSpec
  has no slot for per-task arguments beyond `entity` — Planner only ever
  selects tool NAMES, never fills in structured fields. Wiring alert
  creation onto the graph needs a Task schema extension (an `args: dict`
  field, populated by a QueryAnalyzer/Planner-adjacent extraction step) that
  planning_agent's DB-backed intake will need too — worth solving once, in
  Phase 7.3, not twice.
- `alert_list`/`alert_cancel`/`alert_pause`/`alert_resume` need the calling
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
