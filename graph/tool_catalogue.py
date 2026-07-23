"""
Typed registry wrapping the existing tools/data and tools/analysis functions
for use by Planner/ResearchExecutor (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). None
of the wrapped functions are rewritten — this module only adapts their
heterogeneous signatures (some take a bare ticker, some take another tool's
raw output) to one uniform shape: fn(entity, context) -> raw_result, where
`context` maps a dependency tool's name to its already-fetched raw result.

Scope for Phase 2 is deliberately narrow (single-ticker equity data only,
per the migration plan) — report/mf/screener/alert/planning tools are added
in Phase 7 as each intent is brought onto the graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from tools.data.fetcher import get_info, get_financials, get_ohlcv
from tools.data.news_fetcher import company_news
from tools.analysis.fundamental import compute as compute_fundamental_ratios
from tools.analysis.technical import compute_all as compute_technical_indicators
from tools.analysis.technical import snapshot as technical_snapshot


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
