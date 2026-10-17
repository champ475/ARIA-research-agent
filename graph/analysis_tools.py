"""
graph/analysis_tools.py — peer comparison, risk profile, news sentiment and
backtest as research tools (UX-04).

These analyses existed (tools/analysis/) but no question could reach them,
so "compare TCS with peers" got a made-up answer. Each is now a catalogue
tool whose payload carries its own deterministic `claims`, which
ConclusionBuilder turns into Conclusions like any other evidence.

When they're offered: only on equity/report turns whose wording asks for
one (`triggered_tools`). The Planner adds a triggered tool itself and hides
untriggered ones from the model — every other question's Planner prompt is
unchanged (so recorded eval fixtures stay valid), and "backtest X" can't be
lost to a model that didn't pick the tool.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

import pandas as pd

from config import BACKTEST_COST_PCT

ANALYSIS_INTENTS = {"equity", "report"}

TRIGGERS: dict[str, re.Pattern[str]] = {
    "peer_comparison": re.compile(
        r"\b(peers?|peer[- ]group|competitors?|rivals?|relative valuation|vs\.? (?:its|the) (?:sector|industry))\b",
        re.I,
    ),
    "risk_assessment": re.compile(
        r"\b(risk (?:profile|assessment|score|analysis|factors?|rating|level)|how risky|riskiness|risky)\b",
        re.I,
    ),
    "news_sentiment": re.compile(
        r"\b(sentiment|news (?:tone|mood)|bullish or bearish (?:news|headlines))\b", re.I
    ),
    "backtest": re.compile(
        r"\b(back[- ]?test(?:s|ed|ing)?|(?:ma|moving[- ]average) cross(?:over)?|rsi strateg(?:y|ies))\b", re.I
    ),
    # UX-05: the user's own holdings (no company needed).
    "portfolio_view": re.compile(
        r"\b(my (?:portfolio|holdings|stocks|investments)|portfolio (?:p&l|pnl|performance|value|returns?))\b",
        re.I,
    ),
}
ANALYSIS_TOOLS = frozenset(TRIGGERS)
# Tools that don't take a company: planned once, on the no-entity sentinel.
ENTITYLESS = frozenset({"portfolio_view"})
_TOOL_INTENTS = {"portfolio_view": {"equity", "report", "chat"}}

PAST_PERFORMANCE_NOTE = (
    "Backtests show how a fixed rule would have done on past prices. Past performance doesn't "
    "predict future returns, and real fills, taxes and gaps can differ from the assumptions."
)


def analysis_notes(state: dict) -> str:
    """Deterministic notes an analysis answer must carry: the backtest's
    assumptions and past-performance caveat (never left to the model)."""
    for ev in state.get("evidence") or []:
        p = ev.get("payload")
        if ev.get("evidence_type") == "backtest_result" and isinstance(p, dict) and not p.get("error"):
            return f"\n\n🧪 *Backtest assumptions: {p.get('assumptions')} {PAST_PERFORMANCE_NOTE}*"
    return ""


def triggered_tools(query: str | None, intent: str | None, secondary: list[str] | None = None) -> list[str]:
    """The analysis tools this question explicitly asks for (equity/report turns only)."""
    intents = {intent, *(secondary or [])}
    if not query:
        return []
    return [
        name
        for name, pattern in TRIGGERS.items()
        if intents & _TOOL_INTENTS.get(name, ANALYSIS_INTENTS) and pattern.search(query)
    ]


def _fmt(v: Any, nd: int = 1, suffix: str = "") -> str:
    return "N/A" if v is None else f"{v:,.{nd}f}{suffix}"


# ── peer comparison ──────────────────────────────────────────────────────────

_PEER_FIELDS = ("pe", "pb", "roe", "roce", "de", "mcap_cr")
_PEER_LABELS = {"pe": "P/E", "pb": "P/B", "roe": "ROE", "roce": "ROCE", "de": "D/E"}


def _median(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
    return round(statistics.median(vals), 2) if vals else None


def _live_peers(symbol: str, limit: int) -> dict[str, Any]:
    """Fallback without a snapshot: the curated sector list, quoted live."""
    from config import SECTOR_UNIVERSE
    from tools.analysis.sector_profile import LENDER, sector_profile
    from tools.data.providers import market_data
    from tools.screener import metrics_from_info

    def row(sym: str, info: dict) -> dict:
        m = metrics_from_info(info)
        return {
            "symbol": sym,
            "name": info.get("shortName") or sym,
            **{k: m.get("mcap" if k == "mcap_cr" else k) for k in _PEER_FIELDS},
        }

    info = market_data().quote(symbol)
    sector = info.get("sector") or ""
    group = next((syms for sec, syms in SECTOR_UNIVERSE.items() if sec.lower() == sector.lower()), [])
    peers = []
    for sym in [s for s in group if s != symbol][:limit]:
        try:
            peers.append(row(sym, market_data().quote(sym)))
        except Exception:
            continue
    return {
        "self": row(symbol, info),
        "peers": peers,
        "basis": f"sector: {sector}" if sector else None,
        "lender": sector_profile(info) == LENDER,
        "source": "live (curated list)",
    }


def peer_comparison(symbol: str, limit: int = 5) -> dict[str, Any]:
    """The company next to up to `limit` same-industry peers of similar size
    (snapshot), with peer medians and one claim per ratio."""
    from utils.db import fundamentals_peers

    try:
        found = fundamentals_peers(symbol, limit)
        found["source"] = "nightly fundamentals snapshot"
    except Exception:
        found = {"self": None, "peers": []}
    if not found.get("self"):
        found = _live_peers(symbol, limit)
    me, peers = found["self"], found["peers"]
    lender = bool(found.get("lender") or (me or {}).get("lender"))
    fields = [f for f in ("pe", "pb", "roe", "roce", "de") if not (lender and f in ("roce", "de"))]
    medians = {f: _median(peers, f) for f in fields}
    claims = []
    if me and peers:
        for f in fields:
            mine, med = me.get(f), medians[f]
            if mine is None or med is None:
                continue
            suffix = "%" if f in ("roe", "roce") else ""
            claims.append(
                {
                    "key": f"peers.{f}",
                    "claim": f"{symbol} {_PEER_LABELS[f]} {_fmt(mine, 1, suffix)} vs peer median "
                    f"{_fmt(med, 1, suffix)} ({len(peers)} peers, {found.get('basis')})",
                    "value": mine,
                }
            )
        claims.append(
            {
                "key": "peers.list",
                "claim": f"{symbol} peers compared: " + ", ".join(p["symbol"] for p in peers),
                "value": len(peers),
            }
        )
    return {
        "symbol": symbol,
        "basis": found.get("basis"),
        "source": found.get("source"),
        "lender": lender,
        "rows": ([{**me, "is_self": True}] if me else []) + [{**p, "is_self": False} for p in peers],
        "medians": medians,
        "claims": claims,
        "note": None if peers else "No comparable listed peers found for this company.",
    }


# ── risk profile ─────────────────────────────────────────────────────────────


def risk_profile(
    symbol: str, fundamentals: dict, technicals: dict, news: list[dict] | None
) -> dict[str, Any]:
    from tools.analysis import risk, sentiment

    senti = sentiment.analyse(news or [])
    out = risk.assess(fundamentals or {}, technicals or {}, senti["score"], macro={})
    lender = (fundamentals or {}).get("sector_profile") == "lender"
    not_assessed = ["Macro (neutral baseline)"] + (["Leverage", "Liquidity"] if lender else [])
    claims = [
        {
            "key": "risk.score",
            "claim": f"{symbol} overall risk score {out['score']}/100 ({_strip_emoji(out['level'])})",
            "value": out["score"],
        }
    ]
    for factor, score in out["factors"].items():
        claims.append(
            {"key": f"risk.{factor.lower()}", "claim": f"{symbol} {factor} risk {score}/100", "value": score}
        )
    claims += [
        {"key": "risk.flag", "claim": f"{symbol}: {_strip_emoji(f)}", "value": None} for f in out["flags"][:6]
    ]
    return {
        "symbol": symbol,
        "score": out["score"],
        "level": _strip_emoji(out["level"]),
        "factors": out["factors"],
        "flags": [_strip_emoji(f) for f in out["flags"]],
        "method": "Weighted 0–100 factors: valuation 15%, leverage 20%, liquidity 15%, profitability 15%, "
        "volatility (ATR) 15%, news sentiment 10%, macro 10%. Higher = riskier.",
        "not_assessed": not_assessed,
        "claims": claims,
    }


_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]️?\s*")


def _strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text or "").strip()


# ── news sentiment ───────────────────────────────────────────────────────────


def news_sentiment(symbol: str, news: list[dict] | None) -> dict[str, Any]:
    from tools.analysis import sentiment

    out = sentiment.analyse(news or [])
    n = len(out["items"])
    claims = []
    if n:
        claims.append(
            {
                "key": "sentiment.score",
                "claim": f"{symbol} headline sentiment {out['score']:+.2f} on a −1…+1 scale "
                f"({_strip_emoji(out['label'])}, {n} headlines)",
                "value": out["score"],
            }
        )
        if out["bull_kws"]:
            claims.append(
                {
                    "key": "sentiment.bullish_words",
                    "claim": f"{symbol} bullish words in headlines: "
                    + ", ".join(k for k, _ in out["bull_kws"]),
                    "value": None,
                }
            )
        if out["bear_kws"]:
            claims.append(
                {
                    "key": "sentiment.bearish_words",
                    "claim": f"{symbol} bearish words in headlines: "
                    + ", ".join(k for k, _ in out["bear_kws"]),
                    "value": None,
                }
            )
    return {
        "symbol": symbol,
        "score": out["score"],
        "label": _strip_emoji(out["label"]),
        "headlines_scored": n,
        "method": "Lexicon scoring of headline text (VADER with finance terms when installed); "
        "measures tone, not facts.",
        "claims": claims,
    }


# ── backtest ─────────────────────────────────────────────────────────────────


def backtest(symbol: str, query: str) -> dict[str, Any]:
    from tools.analysis.backtester import ma_crossover_backtest, parse_params, rsi_backtest
    from tools.data.providers import market_data

    params = parse_params(query)
    bars = market_data().ohlcv(symbol, f"{params['years']}y", "1d")
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        return {"symbol": symbol, "params": params, "error": "no price history", "claims": []}
    if params["strategy"] == "rsi":
        result = rsi_backtest(bars, params["rsi_buy"], params["rsi_sell"], cost_pct=BACKTEST_COST_PCT)
    else:
        result = ma_crossover_backtest(bars, params["fast"], params["slow"], cost_pct=BACKTEST_COST_PCT)
    if "error" in result:
        return {"symbol": symbol, "params": params, "error": result["error"], "claims": []}
    m = result["metrics"]
    start, end = str(bars.index[0])[:10], str(bars.index[-1])[:10]
    window = f"{start} to {end}"
    claims = [
        {
            "key": "backtest.total_return",
            "claim": f"{symbol} {result['strategy']} total return {m['total_return_pct']}% over {window} "
            f"vs buy-and-hold {result['buy_hold_pct']}%",
            "value": m["total_return_pct"],
        },
        {"key": "backtest.cagr", "claim": f"{symbol} backtest CAGR {m['cagr_pct']}%", "value": m["cagr_pct"]},
        {
            "key": "backtest.max_drawdown",
            "claim": f"{symbol} backtest max drawdown {m['max_drawdown_pct']}%",
            "value": m["max_drawdown_pct"],
        },
        {
            "key": "backtest.trades",
            "claim": f"{symbol} backtest trades: {m['n_trades']}",
            "value": m["n_trades"],
        },
        {
            "key": "backtest.sharpe",
            "claim": f"{symbol} backtest Sharpe ratio {m['sharpe_ratio']}",
            "value": m["sharpe_ratio"],
        },
    ]
    return {
        "symbol": symbol,
        "strategy": result["strategy"],
        "params": params,
        "window": window,
        "metrics": m,
        "buy_hold_pct": result["buy_hold_pct"],
        "outperformance": result["outperformance"],
        "recent_trades": result["recent_trades"][-5:],
        "assumptions": f"₹1,00,000 start; all-in/all-out on the close; {BACKTEST_COST_PCT}% cost per side "
        "(brokerage, STT, charges, slippage); no tax; split/bonus-adjusted daily closes.",
        "past_performance_note": PAST_PERFORMANCE_NOTE,
        "claims": claims,
    }


# ── portfolio (UX-05) ────────────────────────────────────────────────────────


def portfolio_view(user_id: int | None) -> dict[str, Any]:
    """The user's holdings at live prices, with claims. User-scoped: the
    catalogue marks it non-cacheable (DATA-01)."""
    from tools import portfolio

    if not user_id:
        return {"holdings": [], "summary": {}, "claims": [], "note": "Not signed in."}
    holdings, summary = portfolio.get_live(user_id)
    return {
        "table": portfolio.to_markdown(holdings, summary),
        "summary": {k: (float(v) if not isinstance(v, (list, int)) else v) for k, v in summary.items()},
        "claims": portfolio.claims(holdings, summary),
    }
