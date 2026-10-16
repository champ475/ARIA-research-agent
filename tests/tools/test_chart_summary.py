"""UX-03: every price chart has a text alternative built from its own OHLCV."""

import numpy as np
import pandas as pd
import plotly.io as pio

from tools.charts.builder import candlestick, chart_text_summary, portfolio_chart


def _ohlcv(n: int = 60) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-01", periods=n)
    close = np.linspace(100.0, 130.0, n)
    return pd.DataFrame(
        {"Open": close - 1, "High": close + 2, "Low": close - 3, "Close": close, "Volume": 1000},
        index=idx,
    )


def test_summary_states_period_move_range_and_rsi():
    fig = candlestick(_ohlcv(), "TCS", "Tata Consultancy Services", "1D")
    text = chart_text_summary(fig)
    assert text.startswith("**Chart in words.** Tata Consultancy Services (TCS.NS): 60 sessions")
    assert "from 01 Jan 2026 to 25 Mar 2026" in text
    assert "from ₹100.00 to ₹130.00 (+30.0%)" in text
    assert "low of ₹97.00 and a high of ₹132.00" in text
    assert "RSI(14) is" in text


def test_survives_the_checkpoint_json_round_trip():
    fig = candlestick(_ohlcv(), "TCS")
    assert chart_text_summary(pio.from_json(pio.to_json(fig))) == chart_text_summary(fig)


def test_other_figures_have_no_summary():
    holdings = [{"symbol": "TCS", "current_value": 100.0, "pnl": 5.0, "pnl_pct": 5.0}]
    assert chart_text_summary(portfolio_chart(holdings)) == ""
    assert chart_text_summary(None) == ""
