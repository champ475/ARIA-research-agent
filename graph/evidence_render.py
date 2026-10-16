"""
graph/evidence_render.py — what the answer model actually gets to read (LLM-02).

CommunicationLayer used to pass only each conclusion's one-line claim: fund
lists, movers and deals became counts ("7 matching mutual fund(s) found"),
news became a headline count, filings were cut to 150 words. Asked "best
ELSS funds?" or "why is TCS down?", the model had no names, NAVs or
headlines and either refused or invented them.

`render_evidence_block(evidence, budget_chars, intents, query)` renders each
evidence item in a type-appropriate, size-bounded form:
- tables (funds, movers, deals, screener results): top rows of the relevant
  columns as a compact Markdown table;
- news: up to 8 deduplicated headlines with source and date;
- filings: the passages around results / segment / commentary headings,
  capped per passage;
- pre-rendered Markdown (fund comparison, screener table): kept as is,
  truncated at a row boundary.
Numbers are kept verbatim so the grounding check (LLM-03) can match them.
The per-turn budget is split by relevance (types that fit the intent or the
question get twice the share; unused share is redistributed), and third-party
text is fenced as untrusted (graph/prompt_safety.py).
"""

from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd

from graph.nodes.execute_task import from_cacheable
from graph.prompt_safety import wrap_untrusted

TABLE_ROWS = 15
NEWS_ITEMS = 8
_THIRD_PARTY = {"news_headline", "pdf_extract", "mf_comparison"}

# Columns worth showing per table type, in order (case-insensitive match);
# falls back to the first six columns.
_COLUMNS = {
    "mf_category_funds": [
        "rank",
        "scheme_name",
        "nav",
        "cagr_1y",
        "cagr_3y",
        "cagr_5y",
        "volatility_1y",
        "max_drawdown_3y",
        "consistency_1y",
    ],
    "mf_nav": ["scheme_name", "nav", "nav_date"],
    "top_movers": ["symbol", "ltp", "price", "pct", "pchange", "change", "pct_change", "volume"],
    "bulk_deals": [
        "date",
        "symbol",
        "clientName",
        "client",
        "buySell",
        "buy_sell",
        "quantity",
        "qty",
        "price",
        "wAvgPrice",
    ],
    "block_deals": [
        "date",
        "symbol",
        "clientName",
        "client",
        "buySell",
        "buy_sell",
        "quantity",
        "qty",
        "price",
        "wAvgPrice",
    ],
    "screener_results": [
        "symbol",
        "name",
        "price",
        "pe",
        "pb",
        "roe",
        "roce",
        "de",
        "market_cap",
        "dividend_yield",
    ],
}

# Which evidence types matter most for which intents / question words.
_INTENT_TYPES = {
    "mf": {"mf_category_funds", "mf_nav", "mf_comparison", "news_headline"},
    "screener": {"screener_results", "screener_table", "top_movers"},
    "report": {"pdf_extract", "fundamental_ratio", "news_headline"},
    "equity": {"news_headline", "pdf_extract", "bulk_deals", "block_deals"},
}
_WHY = re.compile(r"\b(why|news|reason|happen|fell|rose|down|up|crash|rally)\b", re.IGNORECASE)

_FILING_HEADINGS = re.compile(
    r"(statement of profit|profit and loss|financial results|segment(?:al)? (?:information|results|revenue)|"
    r"management(?:'s)? discussion|outlook|guidance|revenue from operations|ebitda|net profit|"
    r"key highlights|performance highlights|quarter ended)",
    re.IGNORECASE,
)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.2f}".rstrip("0").rstrip(".") if abs(value) < 1e6 else f"{value:,.0f}"
    text = str(value).replace("|", "/").replace("\n", " ").strip()
    return text[:60] + "…" if len(text) > 60 else text


def _pick_columns(df: pd.DataFrame, evidence_type: str) -> list[str]:
    lower = {c.lower(): c for c in df.columns}
    picked = [lower[c.lower()] for c in _COLUMNS.get(evidence_type, []) if c.lower() in lower]
    return list(dict.fromkeys(picked)) or list(df.columns[:6])


def _markdown_table(df: pd.DataFrame, cols: list[str], budget: int) -> str:
    header = "| " + " | ".join(str(c) for c in cols) + " |\n|" + "---|" * len(cols)
    lines = [header]
    used = len(header)
    for _, row in df.head(TABLE_ROWS).iterrows():
        line = "| " + " | ".join(_fmt(row[c]) for c in cols) + " |"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    shown = len(lines) - 1
    if shown < len(df):
        lines.append(f"(showing {shown} of {len(df)} rows)")
    return "\n".join(lines)


def _as_frame(payload: Any) -> pd.DataFrame | None:
    payload = from_cacheable(payload)
    if isinstance(payload, pd.DataFrame):
        return payload
    if isinstance(payload, list) and payload and all(isinstance(r, dict) for r in payload):
        return pd.DataFrame(payload)
    return None


def _render_news(items: list, budget: int) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    used = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title", "")).split())
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if not title or key in seen:
            continue
        seen.add(key)
        meta = ", ".join(
            x
            for x in (str(item.get("source") or "").strip(), str(item.get("published") or "")[:16].strip())
            if x
        )
        line = f"- {title}" + (f" ({meta})" if meta else "")
        if used + len(line) + 1 > budget or len(lines) >= NEWS_ITEMS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _render_filing(text: str, budget: int) -> str:
    """Passages around the headings that matter, each capped; falls back to
    the opening of the document."""
    if not text:
        return ""
    per = max(300, budget // 3)
    passages: list[str] = []
    used = 0
    last_end = -1
    for m in _FILING_HEADINGS.finditer(text):
        start = max(m.start() - 40, last_end + 1, 0)
        chunk = " ".join(text[start : start + per].split())
        if not chunk or used + len(chunk) > budget:
            break
        passages.append(f"…{chunk}…")
        used += len(chunk) + 4
        last_end = start + per
    if not passages:
        passages.append(" ".join(text[:budget].split()))
    return "\n".join(passages)[:budget]


def _truncate_markdown(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[:budget]
    return cut[: cut.rfind("\n")] + "\n(truncated)" if "\n" in cut else cut + "…"


def render_for_prompt(ev: dict, budget_chars: int) -> str:
    """One evidence item as prompt text of at most `budget_chars` (before the
    heading/fence wrapper), or "" when there's nothing beyond the conclusions."""
    etype = ev.get("evidence_type")
    payload = ev.get("payload")
    if budget_chars <= 0:
        return ""
    if etype == "news_headline" and isinstance(payload, list):
        return _render_news(payload, budget_chars)
    if etype == "pdf_extract" and isinstance(payload, str):
        return "" if payload.lower().startswith("no ") else _render_filing(payload, budget_chars)
    if etype in ("mf_comparison", "screener_table") and isinstance(payload, str):
        return _truncate_markdown(payload, budget_chars)
    if etype in _COLUMNS or etype in ("screener_results",):
        df = _as_frame(payload)
        if df is not None and not df.empty:
            # DATA-04: a ranked list says how it was ranked.
            method = f"Ranking: {df['ranking_method'].iloc[0]}\n" if "ranking_method" in df.columns else ""
            return method + _markdown_table(df, _pick_columns(df, etype), budget_chars - len(method))
    if etype in _ANALYSIS_TYPES and isinstance(payload, dict):
        return _render_analysis(etype, payload, budget_chars)
    if etype == "alert_list" and isinstance(payload, list):
        lines = [
            f"- #{a.get('id')} {a.get('symbol')}: {a.get('condition') or a.get('alert_type')}"
            + ("" if a.get("active", True) else " (paused)")
            for a in payload
            if isinstance(a, dict)
        ]
        return "\n".join(lines)[:budget_chars]
    return ""


# UX-04: the analysis tools' context beyond their claims (which arrive as
# conclusions): the peer table, method notes, backtest assumptions and trades.
_ANALYSIS_TYPES = {"peer_comparison", "risk_profile", "news_sentiment", "backtest_result"}
_PEER_COLUMNS = ["symbol", "name", "pe", "pb", "roe", "roce", "de", "mcap_cr"]


def _render_analysis(etype: str, p: dict, budget: int) -> str:
    if etype == "peer_comparison":
        rows = p.get("rows") or []
        if not rows:
            return str(p.get("note") or "")[:budget]
        df = pd.DataFrame(rows)
        hide = {"roce", "de"} if p.get("lender") else set()
        cols = [c for c in _PEER_COLUMNS if c in df.columns and c not in hide]
        head = f"Peers by {p.get('basis')} (source: {p.get('source')}); first row is {p.get('symbol')}.\n"
        if hide:
            head += "ROCE and D/E aren't meaningful for lenders and are not shown.\n"
        return head + _markdown_table(df, cols, budget - len(head))
    if etype == "backtest_result":
        if p.get("error"):
            return f"Backtest not possible: {p['error']}"[:budget]
        trades = "; ".join(
            f"{t.get('type')} {t.get('date')} @ {t.get('price')}"
            + (f" ({t['pnl_pct']}%)" if "pnl_pct" in t else "")
            for t in p.get("recent_trades") or []
        )
        text = (
            f"Strategy: {p.get('strategy')} | window {p.get('window')} | "
            f"vs buy-and-hold {p.get('buy_hold_pct')}% (difference {p.get('outperformance')} points)\n"
            f"Assumptions: {p.get('assumptions')}\nRecent trades: {trades or 'none'}"
        )
        return text[:budget]
    method = p.get("method") or ""
    missing = p.get("not_assessed") or []
    text = f"Method: {method}" + (f"\nNot assessed: {', '.join(missing)}" if missing else "")
    return text[:budget]


def _weight(ev: dict, intents: set[str], query: str) -> float:
    etype = ev.get("evidence_type")
    if any(etype in _INTENT_TYPES.get(i, set()) for i in intents):
        return 2.0
    if etype == "news_headline" and _WHY.search(query or ""):
        return 2.0
    return 1.0


def render_evidence_block(evidence: list[dict], budget_chars: int, intents: set[str], query: str = "") -> str:
    """All renderable evidence within `budget_chars` total (headings and
    untrusted fences included)."""
    candidates = [ev for ev in evidence or [] if render_for_prompt(ev, 1_000_000)]
    if not candidates or budget_chars <= 0:
        return ""
    weights = [_weight(ev, intents, query) for ev in candidates]
    rendered: list[str | None] = [None] * len(candidates)
    remaining = budget_chars
    pending = list(range(len(candidates)))
    # Water-filling: each pending item gets its weighted share; items that
    # need less give the rest back to the others on the next pass.
    for _ in range(3):
        if not pending or remaining <= 0:
            break
        total_w = sum(weights[i] for i in pending)
        still = []
        for i in pending:
            share = int(remaining * weights[i] / total_w)
            text = _wrap(candidates[i], render_for_prompt(candidates[i], share - 120))
            if text and len(text) <= share:
                rendered[i] = text
            else:
                still.append(i)
        remaining = budget_chars - sum(len(t) for t in rendered if t)
        pending = still if len(still) < len(pending) else []
    if pending:  # last pass: fit what's left as tightly as possible
        total_w = sum(weights[i] for i in pending)
        for i in pending:
            share = int(remaining * weights[i] / total_w)
            text = _wrap(candidates[i], render_for_prompt(candidates[i], share - 120))
            rendered[i] = text if text and len(text) <= share else None
    block = "\n\n".join(t for t in rendered if t)
    return block[:budget_chars]


def _wrap(ev: dict, body: str) -> str:
    if not body:
        return ""
    label = f"{ev.get('entity')} — {str(ev.get('evidence_type')).replace('_', ' ')}"
    if ev.get("evidence_type") in _THIRD_PARTY:
        return f"### {label}\n" + wrap_untrusted(str(ev.get("evidence_type")), body)
    return f"### {label}\n{body}"
