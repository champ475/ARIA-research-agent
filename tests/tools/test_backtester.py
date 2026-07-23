"""Tests for tools/analysis/backtester.py."""
import pandas as pd
import pytest

from tools.analysis import backtester


class TestMetrics:
    def test_empty_series_returns_empty_dict(self):
        assert backtester._metrics(pd.Series(dtype=float)) == {}

    def test_single_value_returns_empty_dict(self):
        assert backtester._metrics(pd.Series([100.0])) == {}

    def test_flat_equity_zero_return(self):
        equity = pd.Series([100_000.0] * 300)
        m = backtester._metrics(equity)
        assert m["total_return_pct"] == 0.0
        assert m["cagr_pct"] == 0.0

    def test_growth_equity_positive_return(self):
        equity = pd.Series([100_000 * (1.001 ** i) for i in range(252)])
        m = backtester._metrics(equity)
        assert m["total_return_pct"] > 0
        assert m["cagr_pct"] > 0

    def test_max_drawdown_nonpositive(self):
        equity = pd.Series([100, 110, 90, 120, 80, 130])
        m = backtester._metrics(equity)
        assert m["max_drawdown_pct"] <= 0


class TestMACrossoverBacktest:
    def test_insufficient_data_returns_error(self, make_ohlcv):
        df = make_ohlcv(n=10)
        out = backtester.ma_crossover_backtest(df, fast=20, slow=50)
        assert "error" in out

    def test_valid_run_returns_expected_shape(self, ohlcv_df):
        out = backtester.ma_crossover_backtest(ohlcv_df, fast=10, slow=30)
        assert "metrics" in out
        assert "buy_hold_pct" in out
        assert out["current_signal"] in {"BUY", "SELL", "FLAT"}

    def test_buy_hold_matches_manual_calc(self, ohlcv_df):
        out = backtester.ma_crossover_backtest(ohlcv_df, fast=10, slow=30)
        df = ohlcv_df.copy()
        df["MA_fast"] = df["Close"].rolling(10).mean()
        df["MA_slow"] = df["Close"].rolling(30).mean()
        df = df.dropna(subset=["MA_fast", "MA_slow"])
        expected = round((float(df["Close"].iloc[-1]) / float(df["Close"].iloc[0]) - 1) * 100, 2)
        assert out["buy_hold_pct"] == expected


class TestRSIBacktest:
    def test_insufficient_data_returns_error(self, make_ohlcv):
        df = make_ohlcv(n=5)
        out = backtester.rsi_backtest(df)
        assert "error" in out

    def test_valid_run_returns_expected_shape(self, ohlcv_df):
        out = backtester.rsi_backtest(ohlcv_df)
        assert "metrics" in out
        assert out["current_signal"] in {"HOLD", "FLAT"}


class TestFormatBacktest:
    def test_error_result_formats_warning(self):
        out = backtester.format_backtest({"error": "no data"})
        assert "⚠️" in out
        assert "no data" in out

    def test_success_result_formats_table(self, ohlcv_df):
        result = backtester.ma_crossover_backtest(ohlcv_df, fast=10, slow=30)
        text = backtester.format_backtest(result)
        assert "Backtest Results" in text
        assert "Sharpe Ratio" in text
