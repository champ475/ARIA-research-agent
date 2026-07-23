"""Tests for agents/report_agent.py::_format_recs — analyst consensus formatting."""
import pandas as pd

from agents.report_agent import _format_recs


def test_none_returns_placeholder():
    assert _format_recs(None) == "No analyst recommendation data available"


def test_empty_df_returns_placeholder():
    assert _format_recs(pd.DataFrame()) == "No analyst recommendation data available"


def test_formats_latest_row():
    df = pd.DataFrame([
        {"strongBuy": 5, "buy": 8, "hold": 2, "sell": 0, "strongSell": 0},
        {"strongBuy": 8, "buy": 12, "hold": 5, "sell": 1, "strongSell": 0},
    ])
    out = _format_recs(df)
    assert "strongBuy=8" in out
    assert "buy=12" in out
    assert "hold=5" in out
    assert "sell=1" in out


def test_missing_columns_handled_gracefully():
    df = pd.DataFrame([{"period": "0m"}])
    assert _format_recs(df) == "No analyst recommendation data available"
