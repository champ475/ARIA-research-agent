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

import hashlib
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import prompts
from graph.alert_spec import (  # noqa: E402  (after the db imports it depends on)
    ALERT_TYPES,
    AlertRefusal,
    AlertSpec,
    alert_id_in,
    already_true,
    confirms_anyway,
    parse_alert,
    spec_from_llm,
)
from tools.analysis.fundamental import compute as compute_fundamental_ratios
from tools.analysis.technical import compute_all as compute_technical_indicators
from tools.analysis.technical import snapshot as technical_snapshot
from tools.data.amfi import get_all_nav, get_category_funds, get_mf_comparison, search_fund
from tools.data.news_fetcher import company_news, google_news
from tools.data.pdf_scraper import get_annual_report_text, get_latest_quarterly_results
from tools.data.providers import market_data
from tools.screener import (
    parse_query as screener_parse_query,
)
from tools.screener import (
    run as run_screener,
)
from tools.screener import (
    to_markdown as screener_to_markdown,
)
from utils.audit import audit
from utils.db import (
    MAX_ACTIVE_ALERTS,  # noqa: E402
    AlertLimitReached,
    DuplicateAlert,
)
from utils.db import (
    alert_add as db_alert_add,
)
from utils.db import (
    alert_list as db_alert_list,
)
from utils.db import (
    alert_pause as db_alert_pause,
)
from utils.db import (
    alert_remove as db_alert_remove,
)
from utils.db import (
    alert_resume as db_alert_resume,
)
from utils.db import (
    screener_save as db_screener_save,
)
from utils.helpers import parse_screener_name
from utils.llm_json import extract_json, record_parse_failure

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
    # DATA-01: the evidence cache is shared across users, so a tool may only be
    # cached if (entity + cache_key(...)) captures EVERY input that changes its
    # output. Side-effecting (write) tools and user-scoped reads are never
    # cached — a cache hit would skip the write, or serve another user's rows.
    side_effect: bool = False
    cacheable: bool = True
    cache_key: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None


def _normalise_text(text: str | None) -> str:
    """Case/whitespace/Unicode-insensitive form of free text, used as a
    cache-key part so trivially different phrasings of one query share a key."""
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().split())


def _key_on_query(entity: str, context: dict[str, Any]) -> dict[str, Any]:
    return {"q": _normalise_text(context.get("_query"))}


def _key_on_parsed_filters(entity: str, context: dict[str, Any]) -> dict[str, Any]:
    blob = json.dumps(context.get("screener_parse_query") or {}, sort_keys=True, default=str)
    return {"filters": hashlib.sha256(blob.encode("utf-8")).hexdigest()}


def _movers_direction(context: dict[str, Any]) -> str:
    return "losers" if "loser" in (context.get("_query") or "").lower() else "gainers"


def _key_on_movers_direction(entity: str, context: dict[str, Any]) -> dict[str, Any]:
    return {"direction": _movers_direction(context)}


# DATA-02: market fetches go through the active provider (yfinance + NSE, or
# FakeProvider under ARIA_FAKE_DATA). Kept as module functions so tests and
# the alert code have one stable name to patch.
def get_info(symbol: str) -> dict:
    return market_data().quote(symbol)


def get_financials(symbol: str) -> dict:
    return market_data().financials(symbol)


def get_ohlcv(symbol: str) -> Any:
    return market_data().ohlcv(symbol)


def get_top_movers(direction: str) -> Any:
    return market_data().movers(direction)


def get_bulk_deals(symbol: str | None) -> Any:
    return market_data().bulk_deals(symbol)


def get_block_deals(symbol: str | None) -> Any:
    return market_data().block_deals(symbol)


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

_EXTRACT_PROMPT = prompts.get("tools.alert_extract")

_ALERT_CREATE_SYSTEM = prompts.get("tools.alert_create_system")


def _get_extract_client(llm_config: dict | None):
    """Built fresh per call (not a cached singleton) since `llm_config`
    varies per user/turn — a BYO-key user's alert-condition extraction must
    use THEIR provider, not a module-wide default that would silently spend
    the app's trial quota on their behalf."""
    from agents.llm_client import LLMClient

    return LLMClient(system_prompt=_ALERT_CREATE_SYSTEM, provider_config=llm_config, node="extract")


def _llm_extract_alert(msg: str, symbol: str, llm_config: dict | None) -> AlertSpec | AlertRefusal | None:
    """Fallback for phrasings the deterministic parser doesn't recognise. The
    model only proposes; graph/alert_spec.py validates it like any other spec."""
    from config import TOKENS_FAST
    from graph.prompt_safety import wrap_untrusted

    prompt = _EXTRACT_PROMPT.format(
        message=wrap_untrusted("user", msg), symbol=symbol, types=list(ALERT_TYPES)
    )
    try:
        raw = _get_extract_client(llm_config).ask(prompt, temperature=0.0, max_tokens=TOKENS_FAST)
        data = extract_json(raw)
    except Exception:
        record_parse_failure("alert_extract", "unusable reply")
        return None
    return spec_from_llm(data, symbol, msg) if isinstance(data, dict) else None


def _current_price(symbol: str) -> float | None:
    try:
        info = get_info(symbol)
    except Exception:
        return None
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    return float(price) if price is not None else None


def _alert_create(entity: str, context: dict[str, Any]) -> dict:
    """VAL-04: the condition is parsed and validated into an AlertSpec before
    anything is written — wrong-number, out-of-range, already-true,
    duplicate and over-the-cap requests are answered instead of saved.
    `entity` is a symbol QueryAnalyzer resolved against the instrument
    master (VAL-02); NO_ENTITY_SENTINEL means no company was named."""
    msg = context.get("_query") or ""
    user_id = context.get("_user_id")
    symbol = entity
    base = {"action": "create", "symbol": symbol}

    if symbol == NO_ENTITY_SENTINEL:
        return {**base, "result": "invalid_symbol"}

    spec = parse_alert(msg, symbol)
    if spec is None:
        spec = _llm_extract_alert(msg, symbol, context.get("_llm_config"))
    if spec is None:
        return {**base, "result": "unrecognized_condition"}
    if isinstance(spec, AlertRefusal):
        return {**base, "result": "invalid_condition", "reason": spec.reason}

    if spec.alert_type in ("price_above", "price_below") and not confirms_anyway(msg):
        price = _current_price(symbol)
        if already_true(spec, price):
            return {
                **base,
                "result": "needs_confirmation",
                "condition": spec.describe(),
                "current_price": price,
            }

    try:
        alert_id = db_alert_add(
            user_id,
            symbol,
            spec.alert_type,
            spec.describe(),
            spec.threshold,
            recurring=spec.recurring,
            cooldown_hours=spec.cooldown_hours,
            direction=spec.direction,
        )
    except DuplicateAlert as e:
        return {**base, "result": "duplicate", "alert_id": e.existing_id, "condition": spec.describe()}
    except AlertLimitReached:
        return {**base, "result": "limit_reached", "limit": MAX_ACTIVE_ALERTS}
    audit(
        "alert.create",
        actor_user_id=user_id,
        target=f"alert:{alert_id}",
        details={"symbol": symbol, "condition": spec.describe(), "recurring": spec.recurring},
    )
    return {
        **base,
        "result": "created",
        "alert_id": alert_id,
        "condition": spec.describe(),
        "recurring": spec.recurring,
    }


def _alert_list(entity: str, context: dict[str, Any]) -> list[dict]:
    """`entity` is unused (ignored) — this tool lists ALL of the calling
    user's alerts, not a per-ticker view. `_user_id` is injected into every
    task's context by research_executor.py (see module docstring)."""
    return db_alert_list(context.get("_user_id"), active_only=False)


def _user_alerts(context: dict[str, Any]) -> list[dict]:
    # `is None`, not `or`: an empty dependency result means "this user has no
    # alerts" — re-querying the database for it was a wasted round trip.
    alerts = context.get("alert_list")
    if alerts is None:
        alerts = db_alert_list(context.get("_user_id"), active_only=False)
    return alerts


def _summary(a: dict) -> dict:
    return {
        "id": a["id"],
        "symbol": a.get("symbol"),
        "condition": a.get("condition"),
        "active": a.get("active"),
    }


def _target_alerts(
    entity: str, context: dict[str, Any], want_active: bool | None
) -> tuple[list[dict], int | None]:
    """The alerts a cancel/pause/resume message points at: "alert #12" picks
    exactly that one of the user's own alerts; otherwise all of theirs on
    `entity` (filtered by active state for pause/resume)."""
    alerts = _user_alerts(context)
    alert_id = alert_id_in(context.get("_query") or "")
    if alert_id is not None:
        return [a for a in alerts if a["id"] == alert_id], alert_id
    matches = [a for a in alerts if a.get("symbol") == entity]
    if want_active is not None:
        matches = [a for a in matches if bool(a.get("active")) == want_active]
    return matches, None


def _manage(entity: str, context: dict[str, Any], action: str, want_active: bool | None, apply) -> dict:
    matches, alert_id = _target_alerts(entity, context, want_active)
    symbol = matches[0]["symbol"] if alert_id is not None and matches else entity
    base = {"action": action, "symbol": symbol}
    if not matches:
        return {**base, "result": "not_found", **({"alert_id": alert_id} if alert_id is not None else {})}
    if len(matches) > 1:
        return {**base, "result": "ambiguous", "matches": [_summary(a) for a in matches]}
    target = matches[0]
    try:
        apply(context.get("_user_id"), target["id"])
    except DuplicateAlert:
        return {**base, "result": "duplicate", "alert_id": target["id"]}
    done = {"cancel": "removed", "pause": "paused", "resume": "resumed"}[action]
    audit(
        f"alert.{action}",
        actor_user_id=context.get("_user_id"),
        target=f"alert:{target['id']}",
        details={"symbol": target.get("symbol"), "condition": target.get("condition")},
    )
    return {**base, "result": done, "alert_id": target["id"], "condition": target.get("condition")}


def _alert_cancel(entity: str, context: dict[str, Any]) -> dict:
    return _manage(entity, context, "cancel", None, db_alert_remove)


def _alert_pause(entity: str, context: dict[str, Any]) -> dict:
    return _manage(entity, context, "pause", True, db_alert_pause)


def _alert_resume(entity: str, context: dict[str, Any]) -> dict:
    return _manage(entity, context, "resume", False, db_alert_resume)


_SCREEN_EXTRACT_PROMPT = prompts.get("tools.screen_extract")


def _llm_screen(query: str, llm_config: dict | None) -> dict | None:
    """VAL-06 fallback for phrasings the grammar couldn't read. The model only
    proposes; every filter is validated against the grammar's schema and
    anything else is dropped."""
    from config import TOKENS_FAST
    from graph.prompt_safety import wrap_untrusted
    from tools.screener_grammar import FIELDS, YF_SECTORS, Screen, ScreenFilter

    prompt = _SCREEN_EXTRACT_PROMPT.format(
        message=wrap_untrusted("user", query), fields=sorted(FIELDS), sectors=sorted(YF_SECTORS)
    )
    try:
        raw = _get_extract_client(llm_config).ask(prompt, temperature=0.0, max_tokens=TOKENS_FAST)
        data = extract_json(raw)
    except Exception:
        record_parse_failure("screener_extract", "unusable reply")
        return None
    if not isinstance(data, dict):
        return None
    screen = Screen(source="llm")
    for f in data.get("filters") or []:
        try:
            field, op = f["field"], f["op"]
            value = float(f["value"])
            high = float(f["high"]) if f.get("high") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        if field in FIELDS and op in ("<", "<=", ">", ">=", "=", "between") and (op != "between" or high):
            screen.filters.append(ScreenFilter(field, op, value, high))
    if data.get("sector") in YF_SECTORS:
        screen.sector = data["sector"]
    return screen.as_dict() if (screen.filters or screen.sector) else None


def _screener_parse_query(entity: str, context: dict[str, Any]) -> dict:
    """Ignores `entity` (typically the NO_ENTITY_SENTINEL "market") — reads
    the user's raw message from `context["_query"]`, injected by
    research_executor.py, since parse_query() needs free text, not a ticker.
    VAL-06: the grammar first; the LLM only when the grammar found nothing."""
    query = context.get("_query") or ""
    parsed = screener_parse_query(query)
    if not parsed.get("filters") and not parsed.get("sector") and parsed.get("not_understood"):
        llm = _llm_screen(query, context.get("_llm_config"))
        if llm is not None:
            llm["not_understood"] = []
            return llm
    return parsed


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
    audit("screener.save", actor_user_id=context.get("_user_id"), target=name, details={"filters": filters})
    return {"name": name, "filters": filters}


def _fetch_top_movers(entity: str, context: dict[str, Any]) -> Any:
    return get_top_movers(_movers_direction(context))


def _fetch_bulk_deals(entity: str, context: dict[str, Any]) -> Any:
    symbol = entity if entity and entity != NO_ENTITY_SENTINEL else None
    return get_bulk_deals(symbol)


def _fetch_block_deals(entity: str, context: dict[str, Any]) -> Any:
    symbol = entity if entity and entity != NO_ENTITY_SENTINEL else None
    return get_block_deals(symbol)


_CATALOGUE: dict[str, ToolSpec] = {}


def _register(spec: ToolSpec) -> None:
    _CATALOGUE[spec.name] = spec


_register(
    ToolSpec(
        name="fetch_info",
        evidence_type="quote_info",
        depends_on=(),
        cost="low",
        fn=_fetch_info,
        description="Fetch current quote/company-info snapshot for a ticker.",
    )
)
_register(
    ToolSpec(
        name="fetch_financials",
        evidence_type="raw_financials",
        depends_on=(),
        cost="low",
        fn=_fetch_financials,
        description="Fetch raw balance sheet / income statement / cash flow for a ticker.",
    )
)
_register(
    ToolSpec(
        name="fetch_ohlcv",
        evidence_type="price_ohlcv",
        depends_on=(),
        cost="low",
        fn=_fetch_ohlcv,
        description="Fetch daily OHLCV price history for a ticker.",
    )
)
_register(
    ToolSpec(
        name="fetch_news",
        evidence_type="news_headline",
        depends_on=(),
        cost="low",
        fn=_fetch_news,
        description="Fetch recent company news headlines for a ticker.",
    )
)
_register(
    ToolSpec(
        name="compute_fundamentals",
        evidence_type="fundamental_ratio",
        depends_on=("fetch_info", "fetch_financials"),
        cost="low",
        fn=_compute_fundamentals,
        description="Recompute PE/PB/ROE/ROCE/D-E etc. from raw financials (never trusts upstream ratios).",
    )
)
_register(
    ToolSpec(
        name="compute_technicals",
        evidence_type="technical_indicator",
        depends_on=("fetch_ohlcv",),
        cost="low",
        fn=_compute_technicals,
        description="Compute RSI/MACD/Bollinger Bands/trend snapshot from OHLCV history.",
    )
)
_register(
    ToolSpec(
        name="fetch_quarterly_results",
        evidence_type="pdf_extract",
        depends_on=(),
        cost="medium",
        fn=_fetch_quarterly_results,
        description="Fetch latest quarterly results text (NSE/BSE filing PDF), for report-domain queries.",
    )
)
_register(
    ToolSpec(
        name="fetch_annual_report_text",
        evidence_type="pdf_extract",
        depends_on=(),
        cost="high",
        fn=_fetch_annual_report_text,
        description="Fetch annual report text (PDF parsing, slow), for initiating-coverage-style report queries.",
    )
)
_register(
    ToolSpec(
        name="fetch_nav",
        evidence_type="mf_nav",
        depends_on=(),
        cost="medium",
        fn=_fetch_nav,
        description="Search AMFI NAV data for mutual funds matching a fund-name search string.",
    )
)
_register(
    ToolSpec(
        name="fetch_category_funds",
        evidence_type="mf_category_funds",
        depends_on=(),
        cost="low",
        fn=_fetch_category_funds,
        description="Fetch top funds (by AMFI NAV) in a mutual fund category, e.g. 'large cap', 'elss'.",
    )
)
_register(
    ToolSpec(
        name="fetch_mf_comparison",
        evidence_type="mf_comparison",
        depends_on=(),
        cost="medium",
        fn=_fetch_mf_comparison,
        description="Compare 2-4 named mutual funds side by side.",
    )
)
_register(
    ToolSpec(
        name="fetch_mf_news",
        evidence_type="news_headline",
        depends_on=(),
        cost="low",
        fn=_fetch_mf_news,
        description="Fetch recent mutual fund industry news for a category or topic.",
    )
)
_register(
    ToolSpec(
        name="alert_create",
        evidence_type="alert_action",
        depends_on=(),
        cost="low",
        fn=_alert_create,
        side_effect=True,
        cacheable=False,
        description="Create a new price/technical-indicator alert on a ticker from the user's natural-language request (e.g. 'alert me if TCS drops below 3500').",
    )
)
_register(
    ToolSpec(
        name="alert_list",
        evidence_type="alert_list",
        depends_on=(),
        cost="low",
        fn=_alert_list,
        cacheable=False,  # per-user rows; the entity is just the 'market' sentinel
        description="List all of the calling user's alerts (active, paused, and triggered).",
    )
)
_register(
    ToolSpec(
        name="alert_cancel",
        evidence_type="alert_action",
        depends_on=("alert_list",),
        cost="low",
        fn=_alert_cancel,
        side_effect=True,
        cacheable=False,
        description="Cancel/remove the calling user's alert on a given ticker.",
    )
)
_register(
    ToolSpec(
        name="alert_pause",
        evidence_type="alert_action",
        depends_on=("alert_list",),
        cost="low",
        fn=_alert_pause,
        side_effect=True,
        cacheable=False,
        description="Pause the calling user's active alert on a given ticker.",
    )
)
_register(
    ToolSpec(
        name="alert_resume",
        evidence_type="alert_action",
        depends_on=("alert_list",),
        cost="low",
        fn=_alert_resume,
        side_effect=True,
        cacheable=False,
        description="Resume the calling user's paused alert on a given ticker.",
    )
)
_register(
    ToolSpec(
        name="screener_parse_query",
        evidence_type="screener_filters",
        depends_on=(),
        cost="low",
        fn=_screener_parse_query,
        cache_key=_key_on_query,
        description="Parse filter criteria (PE/ROE/sector/etc.) from a natural-language screening request.",
    )
)
_register(
    ToolSpec(
        name="screener_run",
        evidence_type="screener_results",
        depends_on=("screener_parse_query",),
        cost="medium",
        fn=_screener_run,
        cache_key=_key_on_parsed_filters,
        description="Run a stock screen across the market universe using parsed filter criteria.",
    )
)
_register(
    ToolSpec(
        name="screener_to_markdown",
        evidence_type="screener_table",
        depends_on=("screener_run",),
        cost="low",
        fn=_screener_to_markdown,
        cacheable=False,  # cheap re-render; depends on two upstream payloads
        description="Render the screen's matching stocks as a Markdown table (use alongside screener_run for a readable result).",
    )
)
_register(
    ToolSpec(
        name="screener_save",
        evidence_type="screener_saved",
        depends_on=("screener_parse_query",),
        cost="low",
        fn=_screener_save,
        side_effect=True,
        cacheable=False,
        description="Save the current screen's filters under a name for reuse.",
    )
)
_register(
    ToolSpec(
        name="fetch_top_movers",
        evidence_type="top_movers",
        depends_on=(),
        cost="low",
        fn=_fetch_top_movers,
        cache_key=_key_on_movers_direction,
        description="Fetch today's top gaining or losing stocks (NSE).",
    )
)
_register(
    ToolSpec(
        name="fetch_bulk_deals",
        evidence_type="bulk_deals",
        depends_on=(),
        cost="low",
        fn=_fetch_bulk_deals,
        description="Fetch bulk deal records, market-wide or for a specific ticker.",
    )
)
_register(
    ToolSpec(
        name="fetch_block_deals",
        evidence_type="block_deals",
        depends_on=(),
        cost="low",
        fn=_fetch_block_deals,
        description="Fetch block deal records, market-wide or for a specific ticker.",
    )
)


def get_tool(name: str) -> ToolSpec:
    return _CATALOGUE[name]


def list_tools() -> list[ToolSpec]:
    return list(_CATALOGUE.values())


def catalogue_summary(exclude: frozenset[str] | set[str] = frozenset()) -> str:
    """Compact text form for inclusion in the Planner's prompt. `exclude`
    hides tools the current turn may not use (SEC-03 write gate)."""
    lines = []
    for spec in _CATALOGUE.values():
        if spec.name in exclude:
            continue
        deps = f" (needs: {', '.join(spec.depends_on)})" if spec.depends_on else ""
        lines.append(f"- {spec.name}: {spec.description}{deps}")
    return "\n".join(lines)
