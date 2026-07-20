"""
tools/analysis/fundamental.py
Recomputes ALL ratios from raw financials — never blindly trusts upstream yfinance values.
Provides full calculation transparency for explainability.
"""

import numpy as np
import pandas as pd
from utils.helpers import safe_float, fmt_num, pct, safe_val


# ── yfinance D/E is reported as % (e.g. 1500 = 150%) for some fields
# We recompute from balance sheet wherever possible.

def compute(info: dict, financials: dict) -> dict:
    d = {}

    def _f(k): return safe_float(info.get(k))

    # ── Raw balance sheet recomputation ──────────────────────────────────────
    # Use explicit None/type check — never use `or` with DataFrames (triggers ambiguity)
    def _df(key):
        v = financials.get(key)
        if v is None or not isinstance(v, pd.DataFrame):
            return pd.DataFrame()
        return v
    bs  = _df("balance_annual")
    inc = _df("income_annual")
    cf  = _df("cashflow_annual")

    raw = _extract_raw(bs, inc, cf)
    d["_raw"] = raw          # exposed for explainability
    d["_source"] = "yfinance NSE (.NS) — balance sheet, income statement, cashflow"

    # ── Price & market ────────────────────────────────────────────────────────
    d["price"]      = _f("currentPrice") or _f("regularMarketPrice")
    d["mktcap"]     = _f("marketCap")
    d["mktcap_fmt"] = fmt_num(d["mktcap"])
    d["w52_high"]   = _f("fiftyTwoWeekHigh")
    d["w52_low"]    = _f("fiftyTwoWeekLow")
    d["volume"]     = _f("averageVolume")
    d["beta"]       = _f("beta")

    # ── Revenue & income ──────────────────────────────────────────────────────
    def _raw_or(key, fallback_key):
        v = raw.get(key)
        return v if v is not None else _f(fallback_key)

    d["revenue"]     = _raw_or("revenue",      "totalRevenue")
    d["revenue_fmt"] = fmt_num(d["revenue"])
    d["net_income"]  = _raw_or("net_income",   "netIncomeToCommon")
    d["ebitda"]      = _raw_or("ebitda",        "ebitda")
    d["ebit"]        = _raw_or("ebit",          "ebit")
    d["fcf"]         = _raw_or("fcf",           "freeCashflow")
    d["operating_cf"]= _raw_or("operating_cf", "operatingCashflow")

    # ── Balance sheet ─────────────────────────────────────────────────────────
    d["total_assets"]   = _raw_or("total_assets",  "totalAssets")
    d["total_equity"]   = _raw_or("total_equity",  "bookValue")
    d["total_debt"]     = raw.get("total_debt")
    d["current_assets"] = raw.get("current_assets")
    d["current_liab"]   = _raw_or("current_liab",  "totalCurrentLiabilities")
    d["cash"]           = _raw_or("cash",           "totalCash")

    # ── D/E ratio — recomputed from balance sheet ─────────────────────────────
    # yfinance sometimes reports D/E as % (multiply by 100).
    # We recompute: D/E = Total Debt / Total Equity (both from balance sheet)
    de_recomputed = _compute_de(raw, info)
    d["de"]            = de_recomputed["value"]
    d["de_calc_detail"]= de_recomputed["detail"]   # for explainability

    # ── Margins (from income statement) ──────────────────────────────────────
    rev = d["revenue"]
    ni  = d["net_income"]
    eb  = d["ebit"]
    ebd = d["ebitda"]

    d["net_margin"]   = _ratio(ni,  rev, "Net Margin  = Net Income / Revenue")
    d["op_margin"]    = _ratio(eb,  rev, "Op Margin   = EBIT / Revenue")
    d["ebitda_margin"]= _ratio(ebd, rev, "EBITDA Margin = EBITDA / Revenue")

    # Fallback to upstream if recomputed is None
    if d["net_margin"] is None:   d["net_margin"]   = _f("profitMargins")
    if d["op_margin"]  is None:   d["op_margin"]    = _f("operatingMargins")
    d["gross_margin"] = _f("grossMargins")

    # ── Profitability ratios ──────────────────────────────────────────────────
    equity = d["total_equity"]
    assets = d["total_assets"]

    # ROE = Net Income / Total Equity
    d["roe"]       = _ratio(ni, equity, "ROE = Net Income / Total Equity")
    if d["roe"] is None: d["roe"] = _f("returnOnEquity")

    # ROA = Net Income / Total Assets
    d["roa"]       = _ratio(ni, assets, "ROA = Net Income / Total Assets")
    if d["roa"] is None: d["roa"] = _f("returnOnAssets")

    # ROCE = EBIT / (Total Assets – Current Liabilities)
    cl = d["current_liab"]
    if eb and assets and cl and (assets - cl) != 0:
        roce_val        = eb / (assets - cl)
        d["roce"]       = round(roce_val * 100, 2)
        d["roce_detail"]= (
            f"ROCE = EBIT / (Total Assets − Current Liabilities)\n"
            f"     = ₹{fmt_num(eb)} / (₹{fmt_num(assets)} − ₹{fmt_num(cl)})\n"
            f"     = ₹{fmt_num(eb)} / ₹{fmt_num(assets - cl)}\n"
            f"     = {d['roce']:.2f}%"
        )
    else:
        d["roce"]       = None
        d["roce_detail"]= "ROCE: insufficient balance sheet data"

    # ── Liquidity ─────────────────────────────────────────────────────────────
    ca = d["current_assets"]
    cl = d["current_liab"]
    if ca and cl and cl != 0:
        d["current_ratio"] = round(ca / cl, 2)
        d["cr_detail"]     = f"Current Ratio = ₹{fmt_num(ca)} / ₹{fmt_num(cl)} = {d['current_ratio']}"
    else:
        d["current_ratio"] = _f("currentRatio")
        d["cr_detail"]     = "Current Ratio: sourced from yfinance info"
    d["quick_ratio"]   = _f("quickRatio")

    # ── Valuation ratios ──────────────────────────────────────────────────────
    price  = d["price"]
    eps    = _f("trailingEps") or _f("forwardEps")
    bv     = _f("bookValue")
    d["eps"]        = eps
    d["book_value"] = bv

    # P/E = Price / EPS
    if price and eps and eps != 0:
        d["pe"]        = round(price / eps, 2)
        d["pe_detail"] = f"P/E = ₹{price} / ₹{eps} = {d['pe']}"
    else:
        d["pe"]        = _f("trailingPE") or _f("forwardPE")
        d["pe_detail"] = "P/E: sourced from yfinance (EPS unavailable for recomputation)"

    # P/B = Price / Book Value per share
    if price and bv and bv != 0:
        d["pb"]        = round(price / bv, 2)
        d["pb_detail"] = f"P/B = ₹{price} / ₹{bv} = {d['pb']}"
    else:
        d["pb"]        = _f("priceToBook")
        d["pb_detail"] = "P/B: sourced from yfinance"

    d["ps"]         = _f("priceToSalesTrailing12Months")
    d["ev_ebitda"]  = _f("enterpriseToEbitda")
    d["peg"]        = _f("pegRatio")
    d["ev"]         = _f("enterpriseValue")

    # ── Growth ────────────────────────────────────────────────────────────────
    d["rev_growth"]   = _f("revenueGrowth")
    d["earn_growth"]  = _f("earningsGrowth")

    # ── Dividends ─────────────────────────────────────────────────────────────
    d["div_yield"]  = _f("dividendYield")
    d["payout"]     = _f("payoutRatio")

    # ── Analyst consensus ─────────────────────────────────────────────────────
    d["target_mean"] = _f("targetMeanPrice")
    d["target_high"] = _f("targetHighPrice")
    d["target_low"]  = _f("targetLowPrice")
    d["rec"]         = info.get("recommendationKey", "N/A")
    d["n_analysts"]  = info.get("numberOfAnalystOpinions", "N/A")

    # Upside to analyst target
    if price and d["target_mean"] and price > 0:
        d["analyst_upside"] = round(((d["target_mean"] - price) / price) * 100, 1)
    else:
        d["analyst_upside"] = None

    # ── Company meta ──────────────────────────────────────────────────────────
    d["name"]        = info.get("longName") or info.get("shortName", "N/A")
    d["sector"]      = info.get("sector", "N/A")
    d["industry"]    = info.get("industry", "N/A")
    d["employees"]   = info.get("fullTimeEmployees", "N/A")
    d["website"]     = info.get("website", "N/A")
    d["description"] = (info.get("longBusinessSummary") or "")[:600]
    d["country"]     = info.get("country", "India")

    # ── DCF (multi-scenario) ──────────────────────────────────────────────────
    d["dcf"]         = _dcf(d)

    # ── Revenue/NI trend ──────────────────────────────────────────────────────
    d["margins_trend"] = _margins_trend(financials)

    return d


# ── Raw extractor from balance sheet / income statement ───────────────────────

def _extract_raw(bs: pd.DataFrame, inc: pd.DataFrame, cf: pd.DataFrame) -> dict:
    raw = {}

    def _get(df, keywords):
        if df is None or df.empty:
            return None
        for row in df.index:
            rs = str(row).lower()
            if any(k in rs for k in keywords):
                try:
                    vals = df.loc[row].dropna()
                    if not vals.empty:
                        return safe_float(vals.iloc[0])
                except Exception:
                    pass
        return None

    # Balance sheet
    raw["total_assets"]   = _get(bs, ["total assets"])
    raw["current_assets"] = _get(bs, ["current assets", "total current assets"])
    raw["current_liab"]   = _get(bs, ["current liabilities", "total current liabilities"])
    raw["total_equity"]   = _get(bs, ["stockholders equity", "total equity", "shareholders equity",
                                       "common stock equity", "total stockholder"])
    raw["total_debt"]     = _get(bs, ["total debt", "long term debt"]) or _get(bs, ["long term debt and capital lease"])
    raw["cash"]           = _get(bs, ["cash and cash equivalents", "cash cash equivalents"])

    # Income statement
    raw["revenue"]        = _get(inc, ["total revenue", "revenue"])
    raw["net_income"]     = _get(inc, ["net income", "net income applicable"])
    raw["ebit"]           = _get(inc, ["ebit", "operating income", "income from operations"])
    raw["ebitda"]         = _get(inc, ["ebitda", "normalized ebitda"])
    raw["gross_profit"]   = _get(inc, ["gross profit"])
    raw["operating_exp"]  = _get(inc, ["operating expense", "total operating expense"])

    # Cashflow
    raw["operating_cf"]   = _get(cf, ["operating cash flow", "total cash from operating"])
    raw["capex"]          = _get(cf, ["capital expenditure", "purchase of property"])
    if raw.get("operating_cf") and raw.get("capex"):
        raw["fcf"] = raw["operating_cf"] - abs(raw["capex"])
    else:
        raw["fcf"] = None

    return raw


def _compute_de(raw: dict, info: dict) -> dict:
    """
    Recompute D/E ratio from balance sheet.
    yfinance sometimes returns D/E as a % (e.g., 1500 = 150%).
    We recompute: D/E = Total Debt / Total Equity.
    """
    debt   = raw.get("total_debt")
    equity = raw.get("total_equity")

    if debt is not None and equity and equity != 0:
        de_val   = round(debt / equity, 2)
        detail   = (
            f"D/E = Total Debt / Total Equity\n"
            f"    = ₹{fmt_num(debt)} / ₹{fmt_num(equity)}\n"
            f"    = {de_val:.2f}x\n"
            f"    Source: Balance sheet (recomputed)"
        )
        return {"value": de_val, "detail": detail}

    # Fallback to yfinance but flag if it looks wrong
    upstream = safe_float(info.get("debtToEquity"))
    if upstream is not None:
        # yfinance sometimes reports as percentage → divide by 100
        if upstream > 20:
            de_val = round(upstream / 100, 2)
            detail = (
                f"D/E = {upstream} (yfinance raw) ÷ 100 = {de_val:.2f}x\n"
                f"    Note: yfinance reports D/E as percentage for some stocks.\n"
                f"    Balance sheet data unavailable for direct recomputation.\n"
                f"    ⚠️ Verify independently — yfinance D/E can be unreliable."
            )
        else:
            de_val = upstream
            detail = (
                f"D/E = {upstream:.2f}x\n"
                f"    Source: yfinance info (balance sheet unavailable)\n"
                f"    ⚠️ Cross-check recommended."
            )
        return {"value": de_val, "detail": detail}

    return {"value": None, "detail": "D/E: balance sheet data unavailable"}


def _ratio(num, denom, formula: str) -> float | None:
    n = safe_float(num)
    d = safe_float(denom)
    if n is None or d is None or d == 0:
        return None
    return round(n / d, 4)


# ── DCF (multi-scenario, transparent assumptions) ─────────────────────────────

def _dcf(d: dict) -> dict | None:
    fcf    = safe_float(d.get("fcf"))
    mktcap = safe_float(d.get("mktcap"))
    price  = safe_float(d.get("price"))

    # Need shares outstanding: derive from mktcap / price
    if not fcf or fcf <= 0:
        return None
    if not mktcap or not price or price == 0:
        return None
    shares = mktcap / price

    scenarios = {
        "Bear": {"growth": 0.06, "wacc": 0.13, "terminal": 0.03},
        "Base": {"growth": 0.10, "wacc": 0.12, "terminal": 0.04},
        "Bull": {"growth": 0.15, "wacc": 0.11, "terminal": 0.05},
    }
    results = {}
    steps   = {}
    for label, s in scenarios.items():
        pv  = 0.0
        cf  = fcf
        yr_detail = []
        for yr in range(1, 6):
            cf  *= (1 + s["growth"])
            pv  += cf / ((1 + s["wacc"]) ** yr)
            yr_detail.append(f"  Y{yr}: FCF=₹{cf/1e7:.1f}Cr PV=₹{(cf/((1+s['wacc'])**yr))/1e7:.1f}Cr")
        tv   = (cf * (1 + s["terminal"])) / (s["wacc"] - s["terminal"])
        tv_pv= tv / ((1 + s["wacc"]) ** 5)
        pv  += tv_pv
        iv   = round(pv / shares, 2)
        results[label] = iv
        steps[label]   = {
            "detail": "\n".join(yr_detail),
            "tv"    : round(tv/1e7, 1),
            "tv_pv" : round(tv_pv/1e7, 1),
            "iv"    : iv,
        }

    return {
        "Bear"        : results["Bear"],
        "Base"        : results["Base"],
        "Bull"        : results["Bull"],
        "price"       : price,
        "upside_base" : round(((results["Base"] - price) / price) * 100, 1) if price else None,
        "fcf_used"    : fcf,
        "shares"      : shares,
        "steps"       : steps,
        "note"        : (
            f"DCF Assumptions: FCF base=₹{fmt_num(fcf)}\n"
            f"Bear: 6% growth, 13% WACC, 3% terminal\n"
            f"Base: 10% growth, 12% WACC, 4% terminal\n"
            f"Bull: 15% growth, 11% WACC, 5% terminal"
        ),
    }


def _margins_trend(financials: dict) -> dict:
    try:
        inc = financials.get("income_annual")
        if inc is None or inc.empty:
            return {}
        rev_rows = [r for r in inc.index if "revenue" in str(r).lower()]
        ni_rows  = [r for r in inc.index if "net income" in str(r).lower()]
        if not rev_rows or not ni_rows:
            return {}
        rev = inc.loc[rev_rows[0]].dropna().head(4).to_dict()
        ni  = inc.loc[ni_rows[0]].dropna().head(4).to_dict()
        return {
            "revenue"   : {str(k)[:10]: round(float(v)/1e7, 1) for k, v in rev.items() if safe_float(v) is not None},
            "net_income": {str(k)[:10]: round(float(v)/1e7, 1) for k, v in ni.items()  if safe_float(v) is not None},
        }
    except Exception:
        return {}


# ── Explainability text builder ───────────────────────────────────────────────

def to_text(d: dict) -> str:
    """Compact text for LLM prompt (all N/A values excluded)."""
    def v(key): return safe_val(d.get(key)) if d.get(key) is not None else "N/A"

    dcf = d.get("dcf") or {}
    mt  = d.get("margins_trend") or {}
    lines = [
        f"**{d.get('name')}** | {d.get('sector')} / {d.get('industry')} | {d.get('country')}",
        f"CMP ₹{v('price')} | 52W H/L ₹{v('w52_high')}/₹{v('w52_low')} | MCap {d.get('mktcap_fmt','N/A')}",
        "",
        f"**Valuation:** P/E {v('pe')} | P/B {v('pb')} | EV/EBITDA {v('ev_ebitda')} | P/S {v('ps')} | PEG {v('peg')}",
        f"**Profitability:** ROE {pct(d.get('roe'))} | ROCE {v('roce')}% | Net Margin {pct(d.get('net_margin'))} | Op Margin {pct(d.get('op_margin'))} | EBITDA Margin {pct(d.get('ebitda_margin'))}",
        f"**Leverage:** D/E {v('de')}x | Current Ratio {v('current_ratio')} | Quick Ratio {v('quick_ratio')}",
        f"**Growth:** Revenue {pct(d.get('rev_growth'))} | Earnings {pct(d.get('earn_growth'))}",
        f"**Per Share:** EPS ₹{v('eps')} | Book Value ₹{v('book_value')} | Div Yield {pct(d.get('div_yield'))} | Beta {v('beta')}",
        f"**Revenue:** {d.get('revenue_fmt','N/A')} | Net Income: {fmt_num(d.get('net_income'))} | FCF: {fmt_num(d.get('fcf'))}",
        f"**Analyst:** {d.get('rec','N/A')} | Target ₹{v('target_mean')} (Low ₹{v('target_low')} / High ₹{v('target_high')}) | N={d.get('n_analysts','N/A')} | Upside {v('analyst_upside')}%",
    ]
    if dcf:
        lines.append(
            f"**DCF:** Bear ₹{dcf.get('Bear','N/A')} | Base ₹{dcf.get('Base','N/A')} (upside {dcf.get('upside_base','N/A')}%) | Bull ₹{dcf.get('Bull','N/A')}"
        )
    if mt.get("revenue"):
        lines.append(f"**Rev trend (Cr):** " + " → ".join(f"₹{v}" for v in list(mt["revenue"].values())[:4]))
    return "\n".join(lines)


def calc_breakdown(d: dict) -> str:
    """Full calculation transparency section for reports."""
    lines = [
        "## 🧮 Calculation Details & Data Sources",
        "",
        f"**Data source:** {d.get('_source', 'yfinance NSE (.NS)')}",
        "",
        "### D/E Ratio",
        d.get("de_calc_detail", "N/A"),
        "",
        "### ROCE",
        d.get("roce_detail", "N/A"),
        "",
        "### Current Ratio",
        d.get("cr_detail", "N/A"),
        "",
        "### P/E Ratio",
        d.get("pe_detail", "N/A"),
        "",
        "### P/B Ratio",
        d.get("pb_detail", "N/A"),
    ]
    dcf = d.get("dcf")
    if dcf:
        lines += ["", "### DCF Model", dcf.get("note", "")]
        for scenario, steps in (dcf.get("steps") or {}).items():
            lines.append(f"\n**{scenario} Case:**")
            lines.append(steps.get("detail", ""))
            lines.append(f"  Terminal Value PV: ₹{steps.get('tv_pv','N/A')}Cr")
            lines.append(f"  **Intrinsic Value: ₹{steps.get('iv','N/A')}**")
    return "\n".join(lines)
