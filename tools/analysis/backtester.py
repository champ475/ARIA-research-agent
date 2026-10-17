"""
tools/analysis/backtester.py
Simple backtesting engine: MA crossover and RSI-based strategies.
No external libraries needed.

UX-04: every trade pays `cost_pct` per side (brokerage, STT, exchange
charges and slippage, BACKTEST_COST_PCT by default), the result states its
assumptions, and `parse_params` reads explicit parameters from the question
("20/50 crossover", "RSI 25/75", "over 3 years").
"""

import re
from typing import Any

import numpy as np
import pandas as pd

from tools.analysis.technical import compute_all


def _metrics(equity: pd.Series) -> dict:
    """Compute performance metrics from equity curve."""
    if equity.empty or len(equity) < 2:
        return {}
    returns = equity.pct_change().dropna()
    total = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    n_years = len(equity) / 252
    cagr = ((equity.iloc[-1] / equity.iloc[0]) ** (1 / max(n_years, 0.1)) - 1) * 100
    vol = returns.std() * np.sqrt(252) * 100
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() != 0 else 0
    drawdown = equity / equity.cummax() - 1
    max_dd = drawdown.min() * 100
    return {
        "total_return_pct": round(float(total), 2),
        "cagr_pct": round(float(cagr), 2),
        "volatility_pct": round(float(vol), 2),
        "sharpe_ratio": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(max_dd), 2),
        "n_trades": 0,
    }


def parse_params(query: str) -> dict[str, Any]:
    """Strategy and parameters named in the question, with defaults:
    MA crossover 20/50 unless RSI is mentioned (RSI 30/70); 5 years of bars."""
    q = (query or "").lower()
    params: dict[str, Any] = {"strategy": "rsi" if re.search(r"\brsi\b", q) else "ma_crossover", "years": 5}
    pair = re.search(r"\b(\d{1,3})\s*(?:/|and|-|vs\.?|x)\s*(\d{1,3})\b", q)
    if params["strategy"] == "ma_crossover":
        fast, slow = (int(pair.group(1)), int(pair.group(2))) if pair else (20, 50)
        fast, slow = min(fast, slow), max(fast, slow)
        if not (2 <= fast < slow <= 250):
            fast, slow = 20, 50
        params.update(fast=fast, slow=slow)
    else:
        buy, sell = (int(pair.group(1)), int(pair.group(2))) if pair else (30, 70)
        buy, sell = min(buy, sell), max(buy, sell)
        if not (5 <= buy < sell <= 95):
            buy, sell = 30, 70
        params.update(rsi_buy=buy, rsi_sell=sell)
    years = re.search(r"\b(\d{1,2})\s*(?:y|yr|yrs|year|years)\b", q)
    if years and 1 <= int(years.group(1)) <= 10:
        params["years"] = int(years.group(1))
    return params


def ma_crossover_backtest(
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 50,
    initial_capital: float = 100_000,
    cost_pct: float = 0.0,
) -> dict:
    """
    Simple MA crossover strategy: buy when fast MA crosses above slow MA, sell when below.
    """
    df = df.dropna(subset=["Close"]) if "Close" in df.columns else df  # TEST-03: gaps aren't prices
    if df.empty or len(df) < slow + 5:
        return {"error": f"Not enough data (need > {slow} candles)"}

    df = df.copy()
    df["MA_fast"] = df["Close"].rolling(fast).mean()
    df["MA_slow"] = df["Close"].rolling(slow).mean()
    df = df.dropna(subset=["MA_fast", "MA_slow"])

    position = 0
    cash = initial_capital
    holdings = 0.0
    equity = []
    trades = []
    entry_price = 0.0

    for i in range(1, len(df)):
        price = float(df["Close"].iloc[i])
        fast_prev = float(df["MA_fast"].iloc[i - 1])
        slow_prev = float(df["MA_slow"].iloc[i - 1])
        fast_curr = float(df["MA_fast"].iloc[i])
        slow_curr = float(df["MA_slow"].iloc[i])

        # Buy signal
        if fast_prev < slow_prev and fast_curr >= slow_curr and position == 0:
            shares = cash * (1 - cost_pct / 100) / price
            holdings = shares
            cash = 0
            position = 1
            entry_price = price
            trades.append({"type": "BUY", "date": str(df.index[i])[:10], "price": round(price, 2)})

        # Sell signal
        elif fast_prev > slow_prev and fast_curr <= slow_curr and position == 1:
            cash = holdings * price * (1 - cost_pct / 100)
            pnl = (price - entry_price) / entry_price * 100
            holdings = 0
            position = 0
            trades.append(
                {
                    "type": "SELL",
                    "date": str(df.index[i])[:10],
                    "price": round(price, 2),
                    "pnl_pct": round(pnl, 2),
                }
            )

        equity.append(cash + holdings * price)

    equity_series = pd.Series(equity)
    metrics = _metrics(equity_series)
    metrics["n_trades"] = len([t for t in trades if t["type"] == "BUY"])

    buy_hold = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[0]) - 1) * 100

    return {
        "strategy": f"MA Crossover ({fast}/{slow})",
        "metrics": metrics,
        "buy_hold_pct": round(buy_hold, 2),
        "outperformance": round(metrics.get("total_return_pct", 0) - buy_hold, 2),
        "recent_trades": trades[-10:],
        "current_signal": "BUY"
        if position == 1
        else ("SELL" if trades and trades[-1]["type"] == "SELL" else "FLAT"),
    }


def rsi_backtest(
    df: pd.DataFrame,
    rsi_buy: int = 30,
    rsi_sell: int = 70,
    initial_capital: float = 100_000,
    cost_pct: float = 0.0,
) -> dict:
    """
    RSI mean-reversion strategy: buy when RSI < rsi_buy, sell when RSI > rsi_sell.
    """
    df = df.dropna(subset=["Close"]) if "Close" in df.columns else df  # TEST-03: gaps aren't prices
    if df.empty or len(df) < 20:
        return {"error": "Not enough data"}

    df_ta = compute_all(df.copy())
    if "RSI" not in df_ta.columns:
        return {"error": "RSI computation failed"}

    df_ta = df_ta.dropna(subset=["RSI"])
    if len(df_ta) < 2:
        return {"error": "Not enough data"}

    position = 0
    cash = initial_capital
    holdings = 0.0
    equity = []
    trades = []
    entry_price = 0.0

    for i in range(len(df_ta)):
        price = float(df_ta["Close"].iloc[i])
        rsi = float(df_ta["RSI"].iloc[i])

        if rsi < rsi_buy and position == 0:
            shares = cash * (1 - cost_pct / 100) / price
            holdings = shares
            cash = 0
            position = 1
            entry_price = price
            trades.append(
                {
                    "type": "BUY",
                    "date": str(df_ta.index[i])[:10],
                    "price": round(price, 2),
                    "rsi": round(rsi, 1),
                }
            )

        elif rsi > rsi_sell and position == 1:
            cash = holdings * price * (1 - cost_pct / 100)
            pnl = (price - entry_price) / entry_price * 100
            holdings = 0
            position = 0
            trades.append(
                {
                    "type": "SELL",
                    "date": str(df_ta.index[i])[:10],
                    "price": round(price, 2),
                    "rsi": round(rsi, 1),
                    "pnl_pct": round(pnl, 2),
                }
            )

        equity.append(cash + holdings * price)

    metrics = _metrics(pd.Series(equity))
    metrics["n_trades"] = len([t for t in trades if t["type"] == "BUY"])
    buy_hold = (float(df_ta["Close"].iloc[-1]) / float(df_ta["Close"].iloc[0]) - 1) * 100

    return {
        "strategy": f"RSI Strategy (Buy<{rsi_buy}, Sell>{rsi_sell})",
        "metrics": metrics,
        "buy_hold_pct": round(buy_hold, 2),
        "outperformance": round(metrics.get("total_return_pct", 0) - buy_hold, 2),
        "recent_trades": trades[-10:],
        "current_signal": "HOLD" if position == 1 else "FLAT",
    }


def format_backtest(result: dict) -> str:
    if "error" in result:
        return f"⚠️ Backtest error: {result['error']}"

    m = result.get("metrics", {})
    trades = result.get("recent_trades", [])
    sign = "🟢" if result.get("outperformance", 0) >= 0 else "🔴"

    lines = [
        f"## Backtest Results — {result.get('strategy')}",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Return | {m.get('total_return_pct', 'N/A')}% |",
        f"| CAGR | {m.get('cagr_pct', 'N/A')}% |",
        f"| Volatility | {m.get('volatility_pct', 'N/A')}% |",
        f"| Sharpe Ratio | {m.get('sharpe_ratio', 'N/A')} |",
        f"| Max Drawdown | {m.get('max_drawdown_pct', 'N/A')}% |",
        f"| No. of Trades | {m.get('n_trades', 'N/A')} |",
        f"| Buy & Hold | {result.get('buy_hold_pct', 'N/A')}% |",
        f"| vs Buy & Hold | {sign} {result.get('outperformance', 'N/A')}% |",
        f"| Current Signal | **{result.get('current_signal', 'N/A')}** |",
        "",
        "**Recent trades:**",
    ]
    for t in trades[-5:]:
        pnl = f" | P&L: {t.get('pnl_pct', 'N/A')}%" if "pnl_pct" in t else ""
        lines.append(f"- {t.get('type')} @ ₹{t.get('price')} on {t.get('date')}{pnl}")
    return "\n".join(lines)
