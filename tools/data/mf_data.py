"""
tools/data/mf_data.py — mutual fund categories, NAV history and rankings (DATA-04).

"Top"/"best" funds used to be the first rows of the AMFI file whose *name*
contained the keyword — effectively alphabetical by AMC, with no
performance data at all. (AMFI also moved NAVAll.txt to an 8-field layout,
which the old parser read as NAV-less rows, so category lookups came back
empty.) Now:

- `parse_navall` reads both the 8-field and the old 6-field layout and the
  SEBI category header above each block, normalised to one canonical name
  ("Equity Scheme - ELSS" and "Equity Schemes - ELSS- Tax Saver Fund" are both
  "ELSS");
- `fetch_history` pulls a scheme's daily NAV history from mfapi.in;
- `compute_metrics` gives point-to-point 1/3/5-year returns (CAGR for 3/5),
  1-year annualised volatility, 3-year maximum drawdown and rolling 1-year
  consistency (share of monthly-stepped 1-year windows over 3 years that
  were positive);
- `rank_funds(category, horizon_years)` ranks direct-growth plans of one SEBI
  category by the CAGR matching the horizon (5y for 5+ years, else 3y, else
  1y), then lower volatility, and says so in `ranking_method`.
Metrics are cached in Postgres (`mf_metrics`, migration 0006) for a day;
scripts/refresh_mf.py warms the common categories.
"""

from __future__ import annotations

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from utils import http

log = logging.getLogger(__name__)

MFAPI_URL = "https://api.mfapi.in/mf/{code}"
_FETCH_WORKERS = 8
RANK_TIME_BUDGET_SECONDS = 18.0  # stays inside the 25 s research-tool budget (REL-02)

# ── categories ────────────────────────────────────────────────────────────────

_HEADER = re.compile(r"^(?P<type>Open|Close|Interval) Ended Schemes\s*\((?P<cat>.*)\)\s*$")
# Canonical names keyed by a normalised fragment of AMFI's sub-category text.
_CANONICAL = [
    (r"elss", "ELSS"),
    (r"large\s*&\s*mid\s*cap", "Large & Mid Cap"),
    (r"large\s*cap", "Large Cap"),
    (r"mid\s*cap", "Mid Cap"),
    (r"small\s*cap", "Small Cap"),
    (r"flexi\s*cap", "Flexi Cap"),
    (r"multi\s*cap", "Multi Cap"),
    (r"focused", "Focused"),
    (r"value", "Value"),
    (r"contra", "Contra"),
    (r"dividend\s*yield", "Dividend Yield"),
    (r"sectoral|thematic", "Sectoral/Thematic"),
    (r"overnight", "Overnight"),
    (r"liquid", "Liquid"),
    (r"money\s*market", "Money Market"),
    (r"ultra\s*short", "Ultra Short Duration"),
    (r"low\s*duration", "Low Duration"),
    (r"medium\s*to\s*long", "Medium to Long Duration"),
    (r"short\s*(?:term|duration)", "Short Duration"),
    (r"medium\s*(?:term|duration)", "Medium Duration"),
    (r"long\s*(?:term|duration)", "Long Duration"),
    (r"dynamic\s*(?:term|bond)", "Dynamic Bond"),
    (r"corporate\s*bond", "Corporate Bond"),
    (r"credit\s*risk", "Credit Risk"),
    (r"banking\s*(?:and|&)\s*psu", "Banking and PSU"),
    (r"constant\s*maturity", "Gilt (10-year Constant Maturity)"),
    (r"gilt", "Gilt"),
    (r"float", "Floater"),
    (r"aggressive\s*hybrid", "Aggressive Hybrid"),
    (r"conservative\s*hybrid", "Conservative Hybrid"),
    (r"balanced\s*advantage|dynamic\s*asset\s*allocation", "Balanced Advantage"),
    (r"balanced\s*hybrid", "Balanced Hybrid"),
    (r"multi\s*asset", "Multi Asset Allocation"),
    (r"arbitrage", "Arbitrage"),
    (r"equity\s*savings", "Equity Savings"),
    (r"retirement", "Retirement"),
    (r"children", "Children's"),
]

# What users type -> canonical category.
CATEGORY_QUERIES = [
    (r"\b(elss|tax\s*sav\w*|80c)\b", "ELSS"),
    (r"\blarge\s*(?:&|and)\s*mid", "Large & Mid Cap"),
    (r"\b(large\s*cap|largecap|blue\s*chip)\b", "Large Cap"),
    (r"\b(mid\s*cap|midcap)\b", "Mid Cap"),
    (r"\b(small\s*cap|smallcap)\b", "Small Cap"),
    (r"\b(flexi\s*cap|flexicap)\b", "Flexi Cap"),
    (r"\b(multi\s*cap|multicap)\b", "Multi Cap"),
    (r"\bfocused\b", "Focused"),
    (r"\bvalue\b", "Value"),
    (r"\bcontra\b", "Contra"),
    (r"\bdividend\s*yield\b", "Dividend Yield"),
    (r"\b(sectoral|thematic)\b", "Sectoral/Thematic"),
    (r"\bindex\b", "Index Fund"),
    (r"\bovernight\b", "Overnight"),
    (r"\bliquid\b", "Liquid"),
    (r"\bmoney\s*market\b", "Money Market"),
    (r"\bultra\s*short\b", "Ultra Short Duration"),
    (r"\bcorporate\s*bond\b", "Corporate Bond"),
    (r"\bbanking\s*(?:and|&)\s*psu\b", "Banking and PSU"),
    (r"\bgilt\b", "Gilt"),
    (r"\bcredit\s*risk\b", "Credit Risk"),
    (r"\b(aggressive\s*hybrid|equity\s*hybrid)\b", "Aggressive Hybrid"),
    (r"\b(balanced\s*advantage|baf|dynamic\s*asset)\b", "Balanced Advantage"),
    (r"\bconservative\s*hybrid\b", "Conservative Hybrid"),
    (r"\barbitrage\b", "Arbitrage"),
    (r"\bmulti\s*asset\b", "Multi Asset Allocation"),
    (r"\b(short\s*(?:term|duration)|debt)\b", "Short Duration"),
]


def canonical_category(header: str) -> str:
    """AMFI's header text (after "Open Ended Schemes(") -> canonical name."""
    text = header.lower()
    if "index" in text and ("index funds - " in text or "other scheme - index" in text):
        return "Index Fund (Debt)" if "debt" in text else "Index Fund"
    if "etf" in text:
        return "ETF"
    if "fund of funds" in text or " fof" in text:
        return "Fund of Funds"
    for pattern, name in _CANONICAL:
        if re.search(pattern, text):
            return name
    sub = header.split(" - ", 1)[-1]
    return re.sub(r"\s*fund\s*$", "", sub, flags=re.IGNORECASE).strip() or header


def category_for_query(query: str) -> str | None:
    for pattern, name in CATEGORY_QUERIES:
        if re.search(pattern, query or "", re.IGNORECASE):
            return name
    return None


# ── NAVAll parsing ────────────────────────────────────────────────────────────


def _plan_option(name: str, plan_field: str = "", option_field: str = "") -> tuple[str, str]:
    text = f"{plan_field} {option_field} {name}".lower()
    plan = "direct" if "direct" in text else "regular"
    if re.search(r"\b(idcw|dividend|payout|reinvest|bonus)\b", text):
        option = "idcw"
    elif "growth" in text:
        option = "growth"
    else:
        option = "other"
    return plan, option


def parse_navall(text: str) -> pd.DataFrame:
    records = []
    category = category_raw = None
    scheme_type = ""
    amc = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _HEADER.match(line)
        if header:
            category_raw = header.group("cat").strip()
            scheme_type = header.group("type").lower()  # open / close / interval
            category = canonical_category(category_raw)
            continue
        parts = [p.strip() for p in line.split(";")]
        if not parts[0].isdigit():
            if ";" not in line and not line.lower().startswith("scheme code"):
                amc = line
            continue
        if len(parts) >= 8:
            name, plan_f, option_f, nav_s, nav_date = parts[3], parts[4], parts[5], parts[6], parts[7]
        elif len(parts) >= 6:
            name, plan_f, option_f, nav_s, nav_date = parts[3], "", "", parts[4], parts[5]
        else:
            continue
        try:
            nav = float(nav_s)
        except ValueError:
            continue  # "N.A." and the like
        plan, option = _plan_option(name, plan_f, option_f)
        display = name if len(parts) < 8 else " - ".join(x for x in (name, plan_f, option_f) if x)
        records.append(
            {
                "scheme_code": parts[0],
                "isin": parts[1] if parts[1] not in ("", "-") else (parts[2] if parts[2] != "-" else ""),
                "amc": amc,
                "category": category,
                "category_raw": category_raw,
                "scheme_type": scheme_type,
                "scheme_name": display,
                "plan": plan,
                "option": option,
                "nav": nav,
                "nav_date": nav_date,
            }
        )
    return pd.DataFrame(records)


# ── NAV history and metrics ───────────────────────────────────────────────────


def fetch_history(code: str) -> pd.Series:
    """Daily NAV, oldest first, indexed by date."""
    resp = http.get(MFAPI_URL.format(code=code), source="mfapi", timeout=(3, 15))
    resp.raise_for_status()
    rows = resp.json().get("data") or []
    points = {}
    for row in rows:
        try:
            points[datetime.strptime(row["date"], "%d-%m-%Y").date()] = float(row["nav"])
        except (KeyError, ValueError, TypeError):
            continue
    return pd.Series(points, dtype=float).sort_index()


def _nav_on_or_before(series: pd.Series, d: date) -> tuple[date, float] | None:
    before = series[series.index <= d]
    if before.empty:
        return None
    return before.index[-1], float(before.iloc[-1])


def _period_return(series: pd.Series, end: date, end_nav: float, years: int) -> float | None:
    """Point-to-point: absolute % for 1 year, CAGR % for 3 and 5 years. None
    if the scheme's history doesn't reach back that far."""
    start = _nav_on_or_before(series, end.replace(year=end.year - years) if not (end.month == 2 and end.day == 29) else end - timedelta(days=365 * years))
    if start is None or start[1] <= 0 or (end - start[0]).days < 365 * years - 7:
        return None
    if years == 1:
        return (end_nav / start[1] - 1) * 100
    span_years = (end - start[0]).days / 365.25
    return ((end_nav / start[1]) ** (1 / span_years) - 1) * 100


def compute_metrics(series: pd.Series) -> dict:
    if series is None or series.empty:
        return {}
    end, end_nav = series.index[-1], float(series.iloc[-1])
    out: dict = {"nav": end_nav, "nav_date": end.isoformat(), "history_start": series.index[0].isoformat()}
    for years in (1, 3, 5):
        value = _period_return(series, end, end_nav, years)
        out[f"cagr_{years}y"] = None if value is None else round(value, 2)

    last_year = series[series.index > end - timedelta(days=365)]
    rets = last_year.pct_change().dropna()
    out["volatility_1y"] = round(float(rets.std() * math.sqrt(252) * 100), 2) if len(rets) > 100 else None

    last3 = series[series.index > end - timedelta(days=365 * 3)]
    if len(last3) > 200:
        peak = last3.cummax()
        out["max_drawdown_3y"] = round(float(((last3 / peak) - 1).min() * 100), 2)
    else:
        out["max_drawdown_3y"] = None

    windows = []
    probe = end
    while probe > end - timedelta(days=365 * 3) and len(windows) < 40:
        r = _period_return(series, probe, float(_nav_on_or_before(series, probe)[1]), 1) if _nav_on_or_before(series, probe) else None
        if r is not None:
            windows.append(r)
        probe -= timedelta(days=30)
    out["consistency_1y"] = round(100 * sum(1 for r in windows if r > 0) / len(windows), 1) if len(windows) >= 12 else None
    return out


# ── ranking ───────────────────────────────────────────────────────────────────


def _metric_for(horizon_years: float | None) -> tuple[str, int]:
    h = 5 if horizon_years is None else horizon_years
    if h >= 5:
        return "cagr_5y", 5
    if h >= 3:
        return "cagr_3y", 3
    return "cagr_1y", 1


def metrics_for(codes: list[str], names: dict[str, str], category: str) -> dict[str, dict]:
    """Cached metrics where fresh; the rest fetched in parallel within the time budget."""
    from utils.db import mf_metrics_get, mf_metrics_put

    try:
        cached = mf_metrics_get(codes, date.today() - timedelta(days=1))
    except Exception as e:
        log.warning(f"[mf] metrics cache unavailable: {e}")
        cached = {}
    missing = [c for c in codes if c not in cached]
    fresh: dict[str, dict] = {}
    if missing:
        deadline = time.monotonic() + RANK_TIME_BUDGET_SECONDS
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix="aria-mf") as pool:
            futures = {pool.submit(fetch_history, code): code for code in missing}
            try:
                for fut in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                    code = futures[fut]
                    try:
                        fresh[code] = compute_metrics(fut.result())
                    except Exception as e:
                        log.info(f"[mf] history for {code} unavailable: {e}")
            except TimeoutError:
                log.warning(f"[mf] metrics for {len(missing) - len(fresh)} {category} fund(s) still pending")
            for fut in futures:
                fut.cancel()
        rows = [{"scheme_code": c, "scheme_name": names.get(c, ""), "category": category, **m} for c, m in fresh.items() if m]
        if rows:
            try:
                mf_metrics_put(rows)
            except Exception as e:
                log.warning(f"[mf] could not cache metrics: {e}")
    return {**cached, **fresh}


def ranking_method(metric: str, years: int) -> str:
    return (
        f"ranked by {years}-year {'return' if years == 1 else 'CAGR'}, then lower 1-year volatility; "
        "direct-growth plans only; past performance doesn't predict future returns"
    )


def rank_funds(query: str, horizon_years: float | None = None, top_n: int = 10, nav_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Direct-growth schemes of the SEBI category named in `query`, ranked by
    the metric matching the horizon. Every row carries `ranking_method`.
    Schemes too young for the chosen period are listed after the ranked ones."""
    category = category_for_query(query)
    if category is None:
        return pd.DataFrame()
    if nav_df is None:
        from tools.data.amfi import get_all_nav

        nav_df = get_all_nav()
    if nav_df.empty or "category" not in nav_df.columns:
        return pd.DataFrame()
    universe = nav_df[
        (nav_df["category"] == category)
        & (nav_df["plan"] == "direct")
        & (nav_df["option"] == "growth")
        # Closed-ended series (e.g. old ELSS "Series V") can't be bought.
        & (nav_df.get("scheme_type", "open") == "open")
    ].drop_duplicates("scheme_code")
    if universe.empty:
        return pd.DataFrame()
    codes = list(universe["scheme_code"])
    metrics = metrics_for(codes, dict(zip(universe["scheme_code"], universe["scheme_name"], strict=False)), category)
    metric, years = _metric_for(horizon_years)
    df = universe.copy()
    for col in ("cagr_1y", "cagr_3y", "cagr_5y", "volatility_1y", "max_drawdown_3y", "consistency_1y"):
        df[col] = [metrics.get(c, {}).get(col) for c in df["scheme_code"]]
    ranked = df[df[metric].notna()].sort_values([metric, "volatility_1y"], ascending=[False, True], na_position="last")
    unranked = df[df[metric].isna()]
    out = pd.concat([ranked, unranked]).head(top_n).reset_index(drop=True)
    out.insert(0, "rank", [i + 1 if i < len(ranked) else None for i in range(len(out))])
    out["ranking_method"] = ranking_method(metric, years)
    return out
