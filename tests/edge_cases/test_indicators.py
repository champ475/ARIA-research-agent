"""TEST-03: analysis functions on degenerate inputs — empty, one row,
NaN-filled and constant series — return something sane instead of raising."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tools.analysis import backtester, fundamental, risk, sentiment, technical


def _bars(closes):
    idx = pd.bdate_range("2026-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0})


SERIES = {
    "empty": [],
    "one_row": [100.0],
    "nan_filled": [np.nan] * 60,
    "constant": [100.0] * 260,
    "short": [100.0 + i for i in range(20)],
}


@pytest.mark.parametrize("name", SERIES)
def test_technicals(name):
    df = technical.compute_all(_bars(SERIES[name]))
    snap = technical.snapshot(df)
    assert isinstance(snap, dict)
    for key in ("RSI", "MACD", "BB_Upper"):
        v = snap.get(key)
        assert v is None or isinstance(v, float)
    technical.detect_patterns(df)
    technical.support_resistance(df)


def test_constant_series_has_no_fake_signal():
    snap = technical.snapshot(technical.compute_all(_bars(SERIES["constant"])))
    assert snap.get("MACD") in (None, 0.0) or abs(snap["MACD"]) < 1e-9
    upper, lower = snap.get("BB_Upper"), snap.get("BB_Lower")
    if upper is not None and lower is not None:
        assert math.isclose(upper, lower)


@pytest.mark.parametrize("name", SERIES)
def test_backtests(name):
    bars = _bars(SERIES[name])
    for out in (backtester.ma_crossover_backtest(bars, 5, 20), backtester.rsi_backtest(bars)):
        assert "error" in out or out["metrics"].get("n_trades", 0) >= 0


def test_fundamentals_risk_and_sentiment_with_nothing():
    d = fundamental.compute({}, {})
    assert d["pe"] is None and d["roce"] is None
    assert isinstance(fundamental.to_text(d), str)
    r = risk.assess({}, {}, 0.0, {})
    assert 0 <= r["score"] <= 100
    assert sentiment.analyse([])["score"] == 0.0
    assert sentiment.analyse([{"title": ""}, {}])["label"]
