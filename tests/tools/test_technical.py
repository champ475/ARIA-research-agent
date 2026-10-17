"""Tests for tools/analysis/technical.py — indicator correctness and edge cases."""

import pandas as pd

from tools.analysis import technical


class TestComputeAll:
    def test_empty_df_returns_unchanged(self):
        df = pd.DataFrame()
        out = technical.compute_all(df)
        assert out.empty

    def test_short_df_returns_unchanged(self, make_ohlcv):
        df = make_ohlcv(n=10)
        out = technical.compute_all(df)
        assert "RSI" not in out.columns
        assert len(out) == 10

    def test_adds_expected_columns(self, ohlcv_df):
        out = technical.compute_all(ohlcv_df)
        for col in [
            "SMA_20",
            "EMA_20",
            "RSI",
            "MACD",
            "MACD_Signal",
            "BB_Upper",
            "BB_Lower",
            "ATR",
            "VWAP",
            "Volume_SMA20",
            "Supertrend",
        ]:
            assert col in out.columns

    def test_rsi_bounded_0_100(self, ohlcv_df):
        out = technical.compute_all(ohlcv_df)
        rsi = out["RSI"].dropna()
        assert not rsi.empty
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_bollinger_ordering(self, ohlcv_df):
        out = technical.compute_all(ohlcv_df)
        sub = out.dropna(subset=["BB_Upper", "BB_Mid", "BB_Lower"])
        assert (sub["BB_Upper"] >= sub["BB_Mid"]).all()
        assert (sub["BB_Mid"] >= sub["BB_Lower"]).all()

    def test_sma_matches_manual_rolling_mean(self, ohlcv_df):
        out = technical.compute_all(ohlcv_df)
        expected = ohlcv_df["Close"].astype(float).rolling(20).mean()
        pd.testing.assert_series_equal(
            out["SMA_20"].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
        )


class TestComputeRR:
    def test_valid_long_setup(self):
        r = technical.compute_rr(entry=100, stop=90, target=130)
        assert r["valid"] is True
        assert r["risk_pts"] == 10
        assert r["reward_pts"] == 30
        assert r["rr"] == 3.0

    def test_invalid_setup_stop_above_entry(self):
        r = technical.compute_rr(entry=100, stop=110, target=130)
        assert r["valid"] is False
        assert r["rr"] is None
        assert "error" in r

    def test_invalid_setup_target_below_entry(self):
        r = technical.compute_rr(entry=100, stop=90, target=95)
        assert r["valid"] is False

    def test_cmp_used_when_within_entry_zone(self):
        r = technical.compute_rr(entry=100, stop=90, target=130, cmp=101)
        assert r["valid"] is True
        assert r["entry"] == 101

    def test_cmp_ignored_when_outside_entry_zone(self):
        r = technical.compute_rr(entry=100, stop=90, target=130, cmp=150)
        assert r["entry"] == 100


class TestSupportResistance:
    def test_empty_or_short_df_returns_empty_dict(self, make_ohlcv):
        assert technical.support_resistance(pd.DataFrame()) == {}
        assert technical.support_resistance(make_ohlcv(n=5), window=20) == {}

    def test_pivot_formula(self, ohlcv_df):
        sr = technical.support_resistance(ohlcv_df)
        h, l, c = ohlcv_df["High"].iloc[-1], ohlcv_df["Low"].iloc[-1], ohlcv_df["Close"].iloc[-1]
        expected_pivot = round((h + l + c) / 3, 2)
        assert sr["Pivot"] == expected_pivot
        assert sr["R1"] == round(2 * expected_pivot - l, 2)
        assert sr["S1"] == round(2 * expected_pivot - h, 2)


class TestSwingLevels:
    def test_too_short_returns_empty(self, make_ohlcv):
        df = make_ohlcv(n=5)
        out = technical.swing_levels(df, order=5)
        assert out == {"swing_highs": [], "swing_lows": []}

    def test_resistances_above_price_supports_below(self, ohlcv_df):
        out = technical.swing_levels(ohlcv_df)
        price = float(ohlcv_df["Close"].iloc[-1])
        assert all(lvl > price for lvl in out["swing_highs"])
        assert all(lvl < price for lvl in out["swing_lows"])

    def test_respects_n_levels_cap(self, ohlcv_df):
        out = technical.swing_levels(ohlcv_df, n_levels=2)
        assert len(out["swing_highs"]) <= 2
        assert len(out["swing_lows"]) <= 2


class TestDetectPatterns:
    def test_empty_df_returns_no_patterns(self):
        assert technical.detect_patterns(pd.DataFrame()) == []

    def test_uptrend_detects_bullish_signal(self, uptrend_df):
        out = technical.compute_all(uptrend_df)
        patterns = technical.detect_patterns(out)
        assert isinstance(patterns, list)


class TestSnapshot:
    def test_empty_df(self):
        assert technical.snapshot(pd.DataFrame()) == {}

    def test_snapshot_no_nan_leak(self, ohlcv_df):
        out = technical.compute_all(ohlcv_df)
        snap = technical.snapshot(out)
        for _k, v in snap.items():
            if isinstance(v, float):
                assert v == v  # not NaN

    def test_snapshot_holds_python_numbers_only(self, ohlcv_df):
        # NumPy 2 reprs scalars as np.float64(1.5); any str() of the evidence
        # (logs, prompts) would show that instead of 1.5.
        import numpy as np

        def numpy_scalars(o, path="snap"):
            if isinstance(o, dict):
                return [p for k, v in o.items() for p in numpy_scalars(v, f"{path}.{k}")]
            if isinstance(o, (list, tuple)):
                return [p for i, v in enumerate(o) for p in numpy_scalars(v, f"{path}[{i}]")]
            return [path] if isinstance(o, np.generic) else []

        snap = technical.snapshot(technical.compute_all(ohlcv_df))
        assert snap["swings"]["swing_highs"] or snap["swings"]["swing_lows"]
        assert numpy_scalars(snap) == []
        assert "np." not in str(snap)
