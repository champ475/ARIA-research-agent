"""
tools/data/amfi.py
AMFI (Association of Mutual Funds in India) free data.
NAV, fund details, top holdings, returns.
"""

import time
import pandas as pd
import logging

from utils import http

log = logging.getLogger(__name__)

AMFI_NAV_URL     = "https://portal.amfiindia.com/spages/NAVAll.txt"  # www.amfiindia.com now redirects here
AMFI_SEARCH_URL  = "https://www.amfiindia.com/modules/FindSchemeDetails"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}

# In-memory NAV cache — AMFI updates once daily; 30-min TTL avoids re-downloading ~2MB on every query
_nav_cache: pd.DataFrame = pd.DataFrame()
_nav_cache_ts: float = 0.0
_NAV_TTL = 1800  # 30 minutes


def get_all_nav() -> pd.DataFrame:
    """
    Download complete NAV data from AMFI for all mutual funds. Columns:
    scheme_code, isin, amc, category (canonical SEBI category), category_raw,
    scheme_name, plan (direct/regular), option (growth/idcw/other), nav, nav_date.

    DATA-04: AMFI's NAVAll is now 8 fields per row (plan/option split out);
    the old 6-field parser kept NAV-less rows, so category lookups came back
    empty. tools/data/mf_data.parse_navall reads both layouts.
    """
    global _nav_cache, _nav_cache_ts
    if not _nav_cache.empty and (time.time() - _nav_cache_ts) < _NAV_TTL:
        return _nav_cache

    try:
        r = http.get(AMFI_NAV_URL, source="amfi", headers=HEADERS, timeout=(3, 20))
        if r.status_code != 200:
            return _nav_cache if not _nav_cache.empty else pd.DataFrame()

        from tools.data.mf_data import parse_navall

        df = parse_navall(r.text)
        _nav_cache    = df
        _nav_cache_ts = time.time()
        return df
    except Exception as e:
        log.warning(f"AMFI NAV download failed: {e}")
        return _nav_cache if not _nav_cache.empty else pd.DataFrame()


def search_fund(query: str, nav_df: pd.DataFrame = None) -> pd.DataFrame:
    """Search funds by name keyword."""
    if nav_df is None or nav_df.empty:
        nav_df = get_all_nav()
    if nav_df.empty:
        return pd.DataFrame()

    q = query.lower()
    mask = nav_df["scheme_name"].str.lower().str.contains(q, na=False)
    return nav_df[mask].head(20)


def get_fund_performance(scheme_name_query: str) -> dict:
    """
    Returns NAV history and computed returns for a fund.
    Uses yfinance as proxy where possible, otherwise AMFI.
    """
    nav_df = get_all_nav()
    if nav_df.empty:
        return {"error": "Could not fetch AMFI data"}

    results = search_fund(scheme_name_query, nav_df)
    if results.empty:
        return {"error": f"No fund found matching '{scheme_name_query}'"}

    # Take the first match
    fund    = results.iloc[0].to_dict()
    nav     = fund.get("nav")
    date    = fund.get("nav_date")
    name    = fund.get("scheme_name")
    amc     = fund.get("amc")

    return {
        "name"    : name,
        "amc"     : amc,
        "nav"     : nav,
        "nav_date": date,
        "code"    : fund.get("scheme_code"),
    }


def get_category_funds(category_keyword: str, top_n: int = 10, horizon_years: float | None = None) -> pd.DataFrame:
    """
    Funds in the SEBI category named by `category_keyword` ("best ELSS",
    "large cap", "flexi cap"...): direct-growth plans ranked by performance,
    each row carrying `ranking_method` (DATA-04, tools/data/mf_data.py).
    An unrecognised keyword falls back to a direct-plan name search.
    """
    from tools.data.mf_data import category_for_query, rank_funds

    nav_df = get_all_nav()
    if nav_df.empty:
        return pd.DataFrame()
    if category_for_query(category_keyword):
        return rank_funds(category_keyword, horizon_years=horizon_years, top_n=top_n, nav_df=nav_df)

    q = category_keyword.lower()
    df = nav_df[nav_df["scheme_name"].str.lower().str.contains(q, na=False)].copy()
    if "plan" in df.columns:
        df = df[(df["plan"] == "direct") & (df["option"] == "growth")]
    return df.dropna(subset=["nav"]).head(top_n)


def format_fund_search_results(df: pd.DataFrame) -> str:
    """Markdown table of funds — with returns, risk and the ranking method
    when the rows came from a ranking (DATA-04)."""
    if df.empty:
        return "No matching funds found."

    cols = ["rank", "scheme_name", "nav", "cagr_1y", "cagr_3y", "cagr_5y", "volatility_1y", "max_drawdown_3y"]
    cols = [c for c in cols if c in df.columns and df[c].notna().any()] or ["amc", "scheme_name", "nav", "nav_date"]
    cols = [c for c in cols if c in df.columns]
    subset = df[cols].head(15).copy()
    labels = {
        "cagr_1y": "1Y Return %",
        "cagr_3y": "3Y CAGR %",
        "cagr_5y": "5Y CAGR %",
        "volatility_1y": "1Y Volatility %",
        "max_drawdown_3y": "3Y Max Drawdown %",
    }
    subset.columns = [labels.get(c, c.replace("_", " ").title()) for c in subset.columns]
    table = subset.to_string(index=False)
    if "ranking_method" in df.columns and len(df):
        table = f"Ranking: {df['ranking_method'].iloc[0]}\n{table}"
    return table


def get_mf_comparison(funds: list[str]) -> str:
    """Compare multiple mutual funds side by side."""
    nav_df = get_all_nav()
    if nav_df.empty:
        return "No fund data available right now."

    from tools.data.mf_data import metrics_for

    found = []
    for fund_name in funds:
        results = search_fund(fund_name, nav_df)
        if "plan" in results.columns:
            preferred = results[(results["plan"] == "direct") & (results["option"] == "growth")]
            results = preferred if not preferred.empty else results
        if not results.empty:
            found.append(results.iloc[0])
    metrics = metrics_for(
        [f["scheme_code"] for f in found], {f["scheme_code"]: f["scheme_name"] for f in found}, "comparison"
    )

    rows = []
    for f in found:
        m = metrics.get(f["scheme_code"], {})
        rows.append({
            "Fund": str(f.get("scheme_name", ""))[:50],
            "Category": f.get("category", "N/A"),
            "NAV": f.get("nav", "N/A"),
            "1Y %": m.get("cagr_1y"),
            "3Y CAGR %": m.get("cagr_3y"),
            "5Y CAGR %": m.get("cagr_5y"),
            "1Y Vol %": m.get("volatility_1y"),
        })

    if not rows:
        return "No fund data found for the requested funds: check the fund names."

    df = pd.DataFrame(rows)
    return "## Mutual Fund Comparison\n\n" + df.to_string(index=False)
