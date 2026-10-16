"""
tools/analysis/fundamental.py
Recomputes ALL ratios from raw financials — never blindly trusts upstream yfinance values.
Provides full calculation transparency for explainability.
"""

import pandas as pd

from tools.analysis.sector_profile import LENDER, LENDER_NOTE, SUPPRESSED, sector_profile
from utils.helpers import fmt_num, pct, safe_float, safe_val

# ── yfinance D/E is reported as % (e.g. 1500 = 150%) for some fields
# We recompute from balance sheet wherever possible.


def compute(info: dict, financials: dict) -> dict:
    d = {}

    def _f(k):
        return safe_float(info.get(k))

    # ── Raw balance sheet recomputation ──────────────────────────────────────
    # Use explicit None/type check — never use `or` with DataFrames (triggers ambiguity)
    def _df(key):
        v = financials.get(key)
        if v is None or not isinstance(v, pd.DataFrame):
            return pd.DataFrame()
        return v

    bs = _df("balance_annual")
    inc = _df("income_annual")
    cf = _df("cashflow_annual")

    raw = _extract_raw(bs, inc, cf)
    d["_raw"] = raw  # exposed for explainability
    d["_source"] = "yfinance NSE (.NS) — balance sheet, income statement, cashflow"

    # ── Price & market ────────────────────────────────────────────────────────
    d["price"] = _f("currentPrice") or _f("regularMarketPrice")
    d["mktcap"] = _f("marketCap")
    d["mktcap_fmt"] = fmt_num(d["mktcap"])
    d["w52_high"] = _f("fiftyTwoWeekHigh")
    d["w52_low"] = _f("fiftyTwoWeekLow")
    d["volume"] = _f("averageVolume")
    d["beta"] = _f("beta")

    # ── Revenue & income ──────────────────────────────────────────────────────
    def _raw_or(key, fallback_key):
        v = raw.get(key)
        return v if v is not None else _f(fallback_key)

    d["revenue"] = _raw_or("revenue", "totalRevenue")
    d["revenue_fmt"] = fmt_num(d["revenue"])
    d["net_income"] = _raw_or("net_income", "netIncomeToCommon")
    d["ebitda"] = _raw_or("ebitda", "ebitda")
    d["ebit"] = _raw_or("ebit", "ebit")
    d["fcf"] = _raw_or("fcf", "freeCashflow")
    d["operating_cf"] = _raw_or("operating_cf", "operatingCashflow")

    # ── Balance sheet ─────────────────────────────────────────────────────────
    d["total_assets"] = _raw_or("total_assets", "totalAssets")
    d["total_equity"] = _raw_or("total_equity", "bookValue")
    d["total_debt"] = raw.get("total_debt")
    d["current_assets"] = raw.get("current_assets")
    d["current_liab"] = _raw_or("current_liab", "totalCurrentLiabilities")
    d["cash"] = _raw_or("cash", "totalCash")

    # ── D/E ratio — recomputed from balance sheet ─────────────────────────────
    # yfinance sometimes reports D/E as % (multiply by 100).
    # We recompute: D/E = Total Debt / Total Equity (both from balance sheet)
    de_recomputed = _compute_de(raw, info)
    d["de"] = de_recomputed["value"]
    d["de_calc_detail"] = de_recomputed["detail"]  # for explainability

    # ── Margins (from income statement) ──────────────────────────────────────
    rev = d["revenue"]
    ni = d["net_income"]
    eb = d["ebit"]
    ebd = d["ebitda"]

    d["net_margin"] = _ratio(ni, rev, "Net Margin  = Net Income / Revenue")
    d["op_margin"] = _ratio(eb, rev, "Op Margin   = EBIT / Revenue")
    d["ebitda_margin"] = _ratio(ebd, rev, "EBITDA Margin = EBITDA / Revenue")

    # Fallback to upstream if recomputed is None
    if d["net_margin"] is None:
        d["net_margin"] = _f("profitMargins")
    if d["op_margin"] is None:
        d["op_margin"] = _f("operatingMargins")
    d["gross_margin"] = _f("grossMargins")

    # ── Profitability ratios ──────────────────────────────────────────────────
    equity = d["total_equity"]
    assets = d["total_assets"]

    # ROE = Net Income / Total Equity
    d["roe"] = _ratio(ni, equity, "ROE = Net Income / Total Equity")
    if d["roe"] is None:
        d["roe"] = _f("returnOnEquity")

    # ROA = Net Income / Total Assets
    d["roa"] = _ratio(ni, assets, "ROA = Net Income / Total Assets")
    if d["roa"] is None:
        d["roa"] = _f("returnOnAssets")

    # ROCE = EBIT / (Total Assets – Current Liabilities)
    cl = d["current_liab"]
    if eb and assets and cl and (assets - cl) != 0:
        roce_val = eb / (assets - cl)
        d["roce"] = round(roce_val * 100, 2)
        d["roce_detail"] = (
            f"ROCE = EBIT / (Total Assets − Current Liabilities)\n"
            f"     = ₹{fmt_num(eb)} / (₹{fmt_num(assets)} − ₹{fmt_num(cl)})\n"
            f"     = ₹{fmt_num(eb)} / ₹{fmt_num(assets - cl)}\n"
            f"     = {d['roce']:.2f}%\n"
            f"     Method: latest fiscal year, consolidated; annual reports may use average capital employed."
        )
    else:
        d["roce"] = None
        d["roce_detail"] = "ROCE: insufficient balance sheet data"

    # ── Liquidity ─────────────────────────────────────────────────────────────
    ca = d["current_assets"]
    cl = d["current_liab"]
    if ca and cl and cl != 0:
        d["current_ratio"] = round(ca / cl, 2)
        d["cr_detail"] = (
            f"Current Ratio = ₹{fmt_num(ca)} / ₹{fmt_num(cl)} = {d['current_ratio']}\n"
            f"     Method: latest fiscal-year balance sheet, consolidated."
        )
    else:
        d["current_ratio"] = _f("currentRatio")
        d["cr_detail"] = "Current Ratio: sourced from yfinance info"
    d["quick_ratio"] = _f("quickRatio")

    # ── Valuation ratios ──────────────────────────────────────────────────────
    price = d["price"]
    eps = _f("trailingEps") or _f("forwardEps")
    bv = _f("bookValue")
    d["eps"] = eps
    d["book_value"] = bv

    # P/E = Price / EPS
    if price and eps and eps != 0:
        d["pe"] = round(price / eps, 2)
        d["pe_detail"] = (
            f"P/E = ₹{price} / ₹{eps} = {d['pe']}\n"
            f"     Method: trailing-12-month consolidated EPS; exchange sites may use standalone EPS."
        )
    else:
        d["pe"] = _f("trailingPE") or _f("forwardPE")
        d["pe_detail"] = "P/E: sourced from yfinance (EPS unavailable for recomputation)"

    # P/B = Price / Book Value per share
    if price and bv and bv != 0:
        d["pb"] = round(price / bv, 2)
        d["pb_detail"] = (
            f"P/B = ₹{price} / ₹{bv} = {d['pb']}\n"
            f"     Method: book value per share from the latest reported balance sheet."
        )
    else:
        d["pb"] = _f("priceToBook")
        d["pb_detail"] = "P/B: sourced from yfinance"

    d["ps"] = _f("priceToSalesTrailing12Months")
    d["ev_ebitda"] = _f("enterpriseToEbitda")
    d["peg"] = _f("pegRatio")
    d["ev"] = _f("enterpriseValue")

    # ── Growth ────────────────────────────────────────────────────────────────
    d["rev_growth"] = _f("revenueGrowth")
    d["earn_growth"] = _f("earningsGrowth")

    # ── Dividends ─────────────────────────────────────────────────────────────
    d["div_yield"] = _f("dividendYield")
    d["payout"] = _f("payoutRatio")

    # ── Analyst consensus ─────────────────────────────────────────────────────
    d["target_mean"] = _f("targetMeanPrice")
    d["target_high"] = _f("targetHighPrice")
    d["target_low"] = _f("targetLowPrice")
    d["rec"] = info.get("recommendationKey", "N/A")
    d["n_analysts"] = info.get("numberOfAnalystOpinions", "N/A")

    # Upside to analyst target
    if price and d["target_mean"] and price > 0:
        d["analyst_upside"] = round(((d["target_mean"] - price) / price) * 100, 1)
    else:
        d["analyst_upside"] = None

    # ── Company meta ──────────────────────────────────────────────────────────
    d["name"] = info.get("longName") or info.get("shortName", "N/A")
    d["sector"] = info.get("sector", "N/A")
    d["industry"] = info.get("industry", "N/A")
    d["employees"] = info.get("fullTimeEmployees", "N/A")
    d["website"] = info.get("website", "N/A")
    d["description"] = (info.get("longBusinessSummary") or "")[:600]
    d["country"] = info.get("country", "India")

    # ── Sector profile (DATA-03) ──────────────────────────────────────────────
    profile = sector_profile(info)
    d["sector_profile"] = profile
    if profile == LENDER:
        for key in SUPPRESSED[LENDER]:
            d[key] = None
        d["roce_detail"] = d["de_calc_detail"] = d["cr_detail"] = LENDER_NOTE
        nii, assets = raw.get("net_interest_income"), d["total_assets"]
        d["nim_proxy"] = _ratio(nii, assets, "NII / Total Assets")
        d["nim_detail"] = (
            f"NII / Total Assets = ₹{fmt_num(nii)} / ₹{fmt_num(assets)} = {pct(d['nim_proxy'])}\n"
            "     Method: a proxy. Reported NIM divides by average interest-earning assets, "
            "so it reads higher."
            if d["nim_proxy"] is not None
            else "NIM: net interest income not in the source data"
        )

    # ── Revenue/NI trend ──────────────────────────────────────────────────────
    d["margins_trend"] = _margins_trend(financials)

    return d


# ── Raw extractor from balance sheet / income statement ───────────────────────


# DATA-03: statement rows are matched by exact name first (in the order
# given), and only then by substring with exclusions. Plain substring
# matching picked "Normalized EBITDA" for EBIT and "Reconciled Cost Of
# Revenue" for revenue, because yfinance lists those rows first.
_ROWS: dict[str, tuple[list[str], list[str], list[str]]] = {
    # field: (exact names, substring fallbacks, exclusions for the fallback)
    "total_assets": (["Total Assets"], ["total assets"], []),
    "current_assets": (["Current Assets", "Total Current Assets"], ["total current assets"], ["other"]),
    "current_liab": (
        ["Current Liabilities", "Total Current Liabilities"],
        ["total current liabilities"],
        ["other"],
    ),
    "total_equity": (
        ["Stockholders Equity", "Common Stock Equity", "Total Equity", "Total Stockholder Equity"],
        ["stockholders equity", "shareholders equity"],
        ["minority", "liabilities"],
    ),
    "total_debt": (
        ["Total Debt", "Long Term Debt And Capital Lease Obligation", "Long Term Debt"],
        ["total debt"],
        ["net debt"],
    ),
    "cash": (["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], [], []),
    "revenue": (["Total Revenue", "Operating Revenue"], ["total revenue"], ["cost"]),
    "net_income": (
        [
            "Net Income",
            "Net Income Common Stockholders",
            "Net Income From Continuing Operation Net Minority Interest",
        ],
        [],
        [],
    ),
    "ebit": (["EBIT", "Operating Income"], [], []),
    "ebitda": (["EBITDA", "Normalized EBITDA"], [], []),
    "gross_profit": (["Gross Profit"], [], []),
    "operating_exp": (["Operating Expense", "Total Operating Expenses"], [], []),
    "net_interest_income": (["Net Interest Income"], [], []),
    "operating_cf": (["Operating Cash Flow", "Total Cash From Operating Activities"], [], []),
    "capex": (["Capital Expenditure", "Purchase Of PPE"], [], []),
}


def _row_value(df: pd.DataFrame, field: str) -> float | None:
    """Latest non-empty value of `field`'s row in `df` (see _ROWS)."""
    if df is None or df.empty:
        return None
    exact, contains, exclude = _ROWS[field]
    by_name = {str(r).strip().lower(): r for r in df.index}
    candidates = [by_name[n.lower()] for n in exact if n.lower() in by_name]
    candidates += [
        r
        for r in df.index
        if any(k in str(r).lower() for k in contains) and not any(x in str(r).lower() for x in exclude)
    ]
    for row in candidates:
        try:
            vals = df.loc[row].dropna()
        except Exception:
            continue
        if isinstance(vals, pd.DataFrame):  # duplicate row labels
            vals = vals.iloc[0].dropna()
        if not vals.empty:
            v = safe_float(vals.iloc[0])
            if v is not None:
                return v
    return None


def _extract_raw(bs: pd.DataFrame, inc: pd.DataFrame, cf: pd.DataFrame) -> dict:
    raw = {}
    for field in ("total_assets", "current_assets", "current_liab", "total_equity", "total_debt", "cash"):
        raw[field] = _row_value(bs, field)
    for field in (
        "revenue",
        "net_income",
        "ebit",
        "ebitda",
        "gross_profit",
        "operating_exp",
        "net_interest_income",
    ):
        raw[field] = _row_value(inc, field)
    raw["operating_cf"] = _row_value(cf, "operating_cf")
    raw["capex"] = _row_value(cf, "capex")
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
    debt = raw.get("total_debt")
    equity = raw.get("total_equity")

    if debt is not None and equity and equity != 0:
        de_val = round(debt / equity, 2)
        detail = (
            f"D/E = Total Debt / Total Equity\n"
            f"    = ₹{fmt_num(debt)} / ₹{fmt_num(equity)}\n"
            f"    = {de_val:.2f}x\n"
            f"    Source: Balance sheet (recomputed)\n"
            f"    Method: total borrowings incl. lease liabilities ÷ shareholders' equity (excl. minority interest)."
        )
        return {"value": de_val, "detail": detail}

    # Fallback to yfinance's figure. DATA-03: checked against 15 companies'
    # balance sheets, `debtToEquity` is always a percentage (TCS 9.83 ==
    # 0.098x, MARUTI 0.096 == 0.001x) — dividing only values above 20, as
    # before, showed TCS at 9.83x.
    upstream = safe_float(info.get("debtToEquity"))
    if upstream is not None:
        de_val = round(upstream / 100, 2)
        detail = (
            f"D/E = {upstream} (yfinance, a percentage) ÷ 100 = {de_val:.2f}x\n"
            f"    Balance sheet data unavailable for direct recomputation.\n"
            f"    Method: Yahoo's most-recent-quarter figure."
        )
        return {"value": de_val, "detail": detail}

    return {"value": None, "detail": "D/E: balance sheet data unavailable"}


def _ratio(num, denom, formula: str) -> float | None:
    n = safe_float(num)
    d = safe_float(denom)
    if n is None or d is None or d == 0:
        return None
    return round(n / d, 4)


def _margins_trend(financials: dict) -> dict:
    try:
        inc = financials.get("income_annual")
        if inc is None or inc.empty:
            return {}
        rev_rows = [r for r in inc.index if "revenue" in str(r).lower()]
        ni_rows = [r for r in inc.index if "net income" in str(r).lower()]
        if not rev_rows or not ni_rows:
            return {}
        rev = inc.loc[rev_rows[0]].dropna().head(4).to_dict()
        ni = inc.loc[ni_rows[0]].dropna().head(4).to_dict()
        return {
            "revenue": {
                str(k)[:10]: round(float(v) / 1e7, 1) for k, v in rev.items() if safe_float(v) is not None
            },
            "net_income": {
                str(k)[:10]: round(float(v) / 1e7, 1) for k, v in ni.items() if safe_float(v) is not None
            },
        }
    except Exception:
        return {}


# ── Explainability text builder ───────────────────────────────────────────────


def to_text(d: dict) -> str:
    """Compact text for LLM prompt (all N/A values excluded)."""

    def v(key):
        return safe_val(d.get(key)) if d.get(key) is not None else "N/A"

    mt = d.get("margins_trend") or {}
    lines = [
        f"**{d.get('name')}** | {d.get('sector')} / {d.get('industry')} | {d.get('country')}",
        f"CMP ₹{v('price')} | 52W H/L ₹{v('w52_high')}/₹{v('w52_low')} | MCap {d.get('mktcap_fmt', 'N/A')}",
        "",
        f"**Valuation:** P/E {v('pe')} [^pe] | P/B {v('pb')} [^pb] | EV/EBITDA {v('ev_ebitda')} | P/S {v('ps')} | PEG {v('peg')}",
        f"**Profitability:** ROE {pct(d.get('roe'))} | ROCE {v('roce')}% [^roce] | Net Margin {pct(d.get('net_margin'))} | Op Margin {pct(d.get('op_margin'))} | EBITDA Margin {pct(d.get('ebitda_margin'))}",
        f"**Leverage:** D/E {v('de')}x [^de] | Current Ratio {v('current_ratio')} [^cr] | Quick Ratio {v('quick_ratio')}",
        f"**Growth:** Revenue {pct(d.get('rev_growth'))} | Earnings {pct(d.get('earn_growth'))}",
        f"**Per Share:** EPS ₹{v('eps')} | Book Value ₹{v('book_value')} | Div Yield {pct(d.get('div_yield'))} | Beta {v('beta')}",
        f"**Revenue:** {d.get('revenue_fmt', 'N/A')} | Net Income: {fmt_num(d.get('net_income'))} | FCF: {fmt_num(d.get('fcf'))}",
        f"**Analyst:** {d.get('rec', 'N/A')} | Target ₹{v('target_mean')} (Low ₹{v('target_low')} / High ₹{v('target_high')}) | N={d.get('n_analysts', 'N/A')} | Upside {v('analyst_upside')}%",
    ]
    if d.get("sector_profile") == LENDER:
        # DATA-03: no ROCE / D/E / current ratio for lenders, not even as N/A.
        lines[4] = (
            f"**Profitability:** ROE {pct(d.get('roe'))} | Net Margin {pct(d.get('net_margin'))} | "
            f"NII/Total Assets (NIM proxy) {pct(d.get('nim_proxy'))} [^nim]"
        )
        lines[5] = f"**Lender note:** {LENDER_NOTE}"
    if mt.get("revenue"):
        lines.append("**Rev trend (Cr):** " + " → ".join(f"₹{v}" for v in list(mt["revenue"].values())[:4]))
    lines.append(
        "(Ratios marked [^pe]/[^pb]/[^roce]/[^de]/[^cr]/[^nim] have a full calculation "
        "on record — keep the marker attached when you state that ratio.)"
    )
    return "\n".join(lines)


# key -> (footnote heading, field in the fundamentals dict holding the derivation)
_CITATION_FIELDS = {
    "pe": ("P/E Ratio", "pe_detail"),
    "pb": ("P/B Ratio", "pb_detail"),
    "roce": ("ROCE", "roce_detail"),
    "de": ("D/E Ratio", "de_calc_detail"),
    "cr": ("Current Ratio", "cr_detail"),
    "nim": ("NIM (proxy)", "nim_detail"),
}


def cited_footnotes(llm_out: str, d: dict) -> str:
    """
    Renders calculation footnotes only for the [^key] markers the model
    actually used in its answer — tracks what was said instead of dumping
    every computable ratio regardless of relevance (see calc_breakdown()
    for the full, unconditional version used elsewhere).
    """
    import re

    keys_cited = re.findall(r"\[\^(\w+)\]", llm_out)
    used = [k for k in _CITATION_FIELDS if k in keys_cited]
    if not used:
        return ""
    lines = ["", "---", "### 🧮 Cited calculations", ""]
    for k in used:
        label, field = _CITATION_FIELDS[k]
        lines.append(f"**[^{k}] {label}**  \n{d.get(field, 'N/A')}")
        lines.append("")
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
    return "\n".join(lines)
