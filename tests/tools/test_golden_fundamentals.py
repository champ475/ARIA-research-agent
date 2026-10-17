"""DATA-03: recomputed ratios against frozen statements for 15 companies (5 IT,
5 banks/NBFCs, 5 others), lenders without ROCE / D/E, and indicators against
textbook definitions on 3 symbols' frozen daily bars.

Fixtures: tests/fixtures/fundamentals/*.json and tests/fixtures/ohlcv/*.csv,
written by scripts/freeze_fundamentals.py (see its docstring and
tests/fixtures/fundamentals/README.md for methodology and known gaps)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tools.analysis import fundamental, technical
from tools.analysis.sector_profile import GENERAL, LENDER, sector_profile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COMPANIES = sorted(p.stem for p in (FIXTURES / "fundamentals").glob("*.json"))
TOL = 0.02  # ±2%


def _load(symbol: str) -> dict:
    return json.loads((FIXTURES / "fundamentals" / f"{symbol}.json").read_text(encoding="utf-8"))


def _frame(rows: dict) -> pd.DataFrame:
    cols = sorted({c for v in rows.values() for c in v}, reverse=True)  # newest first, like yfinance
    return pd.DataFrame({pd.Timestamp(c): {r: v.get(c) for r, v in rows.items()} for c in cols})


def _computed(doc: dict) -> dict:
    fin = {k: _frame(doc[k]) for k in ("income_annual", "balance_annual", "cashflow_annual")}
    return fundamental.compute(doc["info"], fin)


def _close(got, want, *, abs_tol: float = 0.0) -> bool:
    if want is None:
        return got is None
    return got is not None and abs(got - want) <= max(TOL * abs(want), abs_tol)


def test_fixture_set_is_complete():
    docs = [_load(s) for s in COMPANIES]
    lenders = [d["symbol"] for d in docs if d["reference"]["lender"]]
    assert len(docs) == 15 and len(lenders) == 5


@pytest.mark.parametrize("symbol", COMPANIES)
def test_ratios_match_reference_within_2pct(symbol):
    doc = _load(symbol)
    ref, d = doc["reference"], _computed(doc)
    for key in ("pe", "pb", "roe", "net_margin"):
        assert _close(d[key], ref[key], abs_tol=0.0001), (symbol, key, d[key], ref[key])
    if ref["lender"]:
        return
    # D/E is rounded to 2 dp for display, hence the 0.01 absolute slack.
    assert _close(d["de"], ref["de"], abs_tol=0.01), (symbol, "de", d["de"], ref["de"])
    assert _close(d["roce"], ref["roce"]), (symbol, "roce", d["roce"], ref["roce"])
    assert _close(d["current_ratio"], ref["current_ratio"], abs_tol=0.01), (symbol, "cr")


@pytest.mark.parametrize("symbol", COMPANIES)
def test_pe_and_pb_agree_with_yahoos_own_figures(symbol):
    """An outside check: Yahoo's reported trailing P/E and P/B."""
    ref, d = _load(symbol)["reference"], _computed(_load(symbol))
    assert _close(d["pe"], ref["yahoo_pe"]) and _close(d["pb"], ref["yahoo_pb"])


@pytest.mark.parametrize("symbol", COMPANIES)
def test_lenders_never_show_roce_or_de(symbol):
    doc = _load(symbol)
    d = _computed(doc)
    text = fundamental.to_text(d)
    if doc["reference"]["lender"]:
        assert d["sector_profile"] == LENDER
        assert d["roce"] is None and d["de"] is None and d["current_ratio"] is None
        assert "ROCE " not in text.split("**Lender note:**")[0]
        assert "D/E " not in text.split("**Lender note:**")[0]
        assert "NIM proxy" in text and d["nim_proxy"] is not None
    else:
        assert d["sector_profile"] == GENERAL
        assert "ROCE" in text and "D/E" in text


def test_lender_footnote_explains_the_omission():
    d = _computed(_load("SBIN"))
    notes = fundamental.cited_footnotes("ROCE [^roce] and NIM [^nim]", d)
    assert "Not shown for lenders" in notes and "Reported NIM divides by average" in notes


def test_methodology_differences_are_in_the_footnotes():
    d = _computed(_load("TCS"))
    notes = fundamental.cited_footnotes("P/E [^pe], P/B [^pb], ROCE [^roce], D/E [^de], CR [^cr]", d)
    assert notes.count("Method:") == 5


# ── row-matching regressions (the bugs the fixtures caught) ──────────────────


def test_ebit_is_not_read_from_an_ebitda_row():
    inc = pd.DataFrame(
        {pd.Timestamp("2026-03-31"): [900.0, 800.0, 600.0, 2000.0, 400.0]},
        index=["Normalized EBITDA", "EBITDA", "EBIT", "Total Revenue", "Net Income"],
    )
    raw = fundamental._extract_raw(pd.DataFrame(), inc, pd.DataFrame())
    assert raw["ebit"] == 600.0 and raw["ebitda"] == 800.0


def test_revenue_is_not_read_from_cost_of_revenue():
    inc = pd.DataFrame(
        {pd.Timestamp("2026-03-31"): [1500.0, 2000.0]}, index=["Reconciled Cost Of Revenue", "Total Revenue"]
    )
    assert fundamental._extract_raw(pd.DataFrame(), inc, pd.DataFrame())["revenue"] == 2000.0


def test_other_current_assets_is_not_current_assets():
    bs = pd.DataFrame(
        {pd.Timestamp("2026-03-31"): [5.0, 100.0, 50.0]},
        index=["Other Current Assets", "Current Assets", "Current Liabilities"],
    )
    raw = fundamental._extract_raw(bs, pd.DataFrame(), pd.DataFrame())
    assert raw["current_assets"] == 100.0


@pytest.mark.parametrize(
    "info, profile",
    [
        ({"sector": "Financial Services", "industry": "Banks - Regional"}, LENDER),
        ({"sector": "Financial Services", "industry": "Credit Services"}, LENDER),
        ({"sector": "Financial Services", "industry": "Mortgage Finance"}, LENDER),
        ({"sector": "Financial Services", "industry": "Insurance - Life"}, GENERAL),
        ({"sector": "Financial Services", "industry": "Asset Management"}, GENERAL),
        ({"sector": "Technology", "industry": "Information Technology Services"}, GENERAL),
        ({}, GENERAL),
    ],
)
def test_sector_profile(info, profile):
    assert sector_profile(info) == profile


def test_screener_metrics_follow_the_lender_switch_and_percent_de():
    from tools.screener import metrics_from_info

    bank = {**_load("SBIN")["info"], "debtToEquity": 300.0}
    assert metrics_from_info(bank)["de"] is None and metrics_from_info(bank)["roce"] is None
    tcs = _load("TCS")["info"]
    assert metrics_from_info(tcs)["de"] == round(tcs["debtToEquity"] / 100, 2)


# ── indicators vs textbook definitions ────────────────────────────────────────


def _bars(symbol: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "ohlcv" / f"{symbol}.csv", index_col="Date", parse_dates=True)


def _reference_indicators(close: pd.Series) -> dict[str, pd.Series]:
    """Wilder's RSI(14), MACD(12, 26, 9) on EMAs, Bollinger(20, 2) with the
    population standard deviation — the published definitions."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    macd = (
        close.ewm(span=12, min_periods=12, adjust=False).mean()
        - close.ewm(span=26, min_periods=26, adjust=False).mean()
    )
    mid = close.rolling(20).mean()
    sd = close.rolling(20).apply(lambda w: np.std(w, ddof=0), raw=True)
    return {
        "RSI": 100 - 100 / (1 + up / down),
        "MACD": macd,
        "MACD_Signal": macd.ewm(span=9, min_periods=9, adjust=False).mean(),
        "BB_Upper": mid + 2 * sd,
        "BB_Lower": mid - 2 * sd,
    }


@pytest.mark.parametrize("symbol", ["TCS", "HDFCBANK", "RELIANCE"])
@pytest.mark.parametrize("use_ta", [True, False])
def test_indicators_match_textbook_definitions(symbol, use_ta, monkeypatch):
    bars = _bars(symbol)
    assert len(bars) >= 200
    monkeypatch.setattr(technical, "TA_OK", use_ta and technical.TA_OK)
    out = technical.compute_all(bars)
    ref = _reference_indicators(bars["Close"].astype(float))
    for col, series in ref.items():
        tail_got, tail_want = out[col].iloc[-120:], series.iloc[-120:]
        assert np.allclose(tail_got, tail_want, rtol=1e-3, atol=1e-3), (symbol, col, use_ta)
