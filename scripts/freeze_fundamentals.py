"""
scripts/freeze_fundamentals.py — freeze statement fixtures for the golden
fundamentals tests (DATA-03).

    python scripts/freeze_fundamentals.py            # all 15 companies
    python scripts/freeze_fundamentals.py TCS SBIN   # just these
    python scripts/freeze_fundamentals.py --references   # recompute references, offline
    python scripts/freeze_fundamentals.py --ohlcv        # 1y daily bars for the indicator tests
    python scripts/freeze_fundamentals.py --merge-info   # add new INFO_FIELDS only, statements untouched

For each company writes tests/fixtures/fundamentals/<SYMBOL>.json with:
- `info`: the quote fields the ratio code reads, as Yahoo reported them;
- `income_annual` / `balance_annual` / `cashflow_annual`: the latest two
  fiscal years, {row: {date: value}};
- `frozen_at`;
- `reference`: the expected ratios, worked out by `reference()` below with
  plain formulas on exactly-named rows — written separately from
  tools/analysis/fundamental.py on purpose, so a matching bug there (EBIT
  read from an EBITDA row) shows up as a mismatch. Lenders get no ROCE, D/E
  or current ratio. P/E and P/B also carry Yahoo's own reported figures
  (`yahoo_pe`, `yahoo_pb`) as an outside check.

Re-run only to refresh the set (e.g. after a new fiscal year); the tests
read the frozen files and never touch the network.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("freeze_fundamentals")

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fundamentals"
OHLCV_OUT = OUT.parent / "ohlcv"
OHLCV_SYMBOLS = ["TCS", "HDFCBANK", "RELIANCE"]

COMPANIES = {
    "it": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "lenders": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "BAJFINANCE"],
    "others": ["RELIANCE", "ITC", "LT", "MARUTI", "SUNPHARMA"],
}

INFO_FIELDS = [
    "longName",
    "sector",
    "industry",
    "currentPrice",
    "regularMarketPrice",
    "marketCap",
    "trailingEps",
    "bookValue",
    "trailingPE",
    "priceToBook",
    "returnOnEquity",
    "debtToEquity",
    "currentRatio",
    "profitMargins",
    "totalRevenue",
    # v3 CORR-01 / EVAL-01: dividend and analyst fields the answer eval checks.
    "dividendYield",
    "dividendRate",
    "trailingAnnualDividendYield",
    "payoutRatio",
    "targetMeanPrice",
    "numberOfAnalystOpinions",
]


def _frame(df) -> dict:
    if df is None or df.empty:
        return {}
    df = df.iloc[:, :2]
    out: dict = {}
    for row in df.index:
        vals = {str(col)[:10]: float(v) for col, v in df.loc[row].items() if v == v and v is not None}
        if vals:
            out[str(row)] = vals
    return out


LENDER_INDUSTRIES = {"Banks - Regional", "Banks - Diversified", "Credit Services", "Mortgage Finance"}


def _latest(frame: dict, row: str) -> float | None:
    vals = frame.get(row) or {}
    return vals[max(vals)] if vals else None


def _div(a: float | None, b: float | None) -> float | None:
    return round(a / b, 6) if a is not None and b else None


def reference(doc: dict) -> dict:
    info, inc, bs = doc["info"], doc["income_annual"], doc["balance_annual"]
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    ni = _latest(inc, "Net Income")
    revenue = _latest(inc, "Total Revenue")
    equity = _latest(bs, "Stockholders Equity")
    assets = _latest(bs, "Total Assets")
    lender = info.get("industry") in LENDER_INDUSTRIES
    ref = {
        "lender": lender,
        "pe": _div(price, info.get("trailingEps")),
        "pb": _div(price, info.get("bookValue")),
        "roe": _div(ni, equity),
        "net_margin": _div(ni, revenue),
        "yahoo_pe": info.get("trailingPE"),
        "yahoo_pb": info.get("priceToBook"),
    }
    if not lender:
        cl = _latest(bs, "Current Liabilities")
        ebit = _latest(inc, "EBIT")
        ref["roce"] = round(100 * ebit / (assets - cl), 4) if ebit and assets and cl else None
        ref["de"] = _div(_latest(bs, "Total Debt"), equity)
        ref["current_ratio"] = _div(_latest(bs, "Current Assets"), cl)
    return ref


def freeze(symbol: str) -> Path:
    import yfinance as yf

    t = yf.Ticker(f"{symbol}.NS")
    info = t.info or {}
    doc = {
        "symbol": symbol,
        "info": {k: info.get(k) for k in INFO_FIELDS if info.get(k) is not None},
        "income_annual": _frame(t.income_stmt),
        "balance_annual": _frame(t.balance_sheet),
        "cashflow_annual": _frame(t.cashflow),
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    doc["reference"] = reference(doc)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{symbol}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def merge_info(symbol: str) -> Path:
    """Add the INFO_FIELDS a fixture lacks, from today's quote, leaving the
    frozen statements, prices and references untouched."""
    import yfinance as yf

    path = OUT / f"{symbol}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    info = yf.Ticker(f"{symbol}.NS").info or {}
    for k in INFO_FIELDS:
        if k not in doc["info"] and info.get(k) is not None:
            doc["info"][k] = info[k]
    doc["info_merged_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def freeze_ohlcv(symbol: str) -> Path:
    import yfinance as yf

    df = yf.Ticker(f"{symbol}.NS").history(period="1y", interval="1d", auto_adjust=True)
    df = df[["Open", "High", "Low", "Close", "Volume"]].round(4)
    df.index = df.index.tz_localize(None).date
    df.index.name = "Date"
    OHLCV_OUT.mkdir(parents=True, exist_ok=True)
    path = OHLCV_OUT / f"{symbol}.csv"
    df.to_csv(path)
    return path


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if argv[:1] == ["--references"]:
        for path in sorted(OUT.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["reference"] = reference(doc)
            path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            log.info("references for %s", path.stem)
        return 0
    if argv[:1] == ["--ohlcv"]:
        for sym in OHLCV_SYMBOLS:
            log.info("froze %s", freeze_ohlcv(sym).name)
        return 0
    if argv[:1] == ["--merge-info"]:
        for sym in argv[1:] or [s for group in COMPANIES.values() for s in group]:
            try:
                log.info("merged %s", merge_info(sym).name)
            except Exception as e:  # keep going; report at the end
                log.error("%s failed: %s", sym, type(e).__name__)
        return 0
    symbols = argv or [s for group in COMPANIES.values() for s in group]
    for s in symbols:
        try:
            log.info("froze %s", freeze(s).name)
        except Exception as e:  # keep going; report at the end
            log.error("%s failed: %s", s, type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
