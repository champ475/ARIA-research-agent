"""
tools/charts/builder.py
TradingView-style dark theme Plotly charts.
Returns a plotly.graph_objects.Figure, or None on insufficient data — never a string.
Callers feed these straight into gr.Plot().
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tools.analysis.technical import compute_all

BG = "#131722"
PANEL = "#1e2130"
GRID = "#2a2e39"
TEXT = "#d1d4dc"
GREEN = "#26a69a"
RED = "#ef5350"
BLUE = "#2962ff"
AMBER = "#f6c90e"
ORANGE = "#ff6d00"
PURPLE = "#ab47bc"
CHART_CFG = {
    "displayModeBar": True,
    "scrollZoom": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    "displaylogo": False,
}


def _layout(title: str, height: int = 650) -> dict:
    return dict(
        title=dict(text=title, font=dict(color=TEXT, size=13), x=0.01),
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color=TEXT, size=11),
        xaxis_rangeslider_visible=False,
        margin=dict(l=60, r=20, t=40, b=20),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="right",
            x=1,
            bgcolor="rgba(19,23,34,0.8)",
            font=dict(size=10),
        ),
        height=height,
        hovermode="x unified",
        hoverlabel=dict(bgcolor=PANEL, font=dict(color=TEXT, size=11)),
    )


def _axis_style(row_i=1) -> dict:
    return dict(
        showgrid=True,
        gridcolor=GRID,
        gridwidth=0.5,
        zeroline=False,
        showline=False,
        tickfont=dict(color=TEXT, size=10),
        row=row_i,
        col=1,
    )


def candlestick(
    df: pd.DataFrame,
    symbol: str,
    company: str = "",
    timeframe: str = "1D",
    indicators: list = None,
):
    """Full TradingView-style candlestick chart with indicators. Returns None on insufficient data."""
    if indicators is None:
        indicators = ["BB", "SMA50", "SMA200", "RSI", "MACD"]

    if df.empty or len(df) < 5:
        return None

    df = compute_all(df.copy())

    show_rsi = "RSI" in indicators
    show_macd = "MACD" in indicators
    n_rows = 2 + int(show_rsi) + int(show_macd)

    row_h = [0.55, 0.15]
    if show_rsi:
        row_h.append(0.15)
    if show_macd:
        row_h.append(0.15)
    total = sum(row_h)
    row_h = [r / total for r in row_h]

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=row_h,
        specs=[[{"secondary_y": False}]] * n_rows,
    )

    # ── Candlestick ───────────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            increasing_fillcolor=GREEN,
            decreasing_fillcolor=RED,
            name="Price",
            showlegend=False,
            line=dict(width=1),
        ),
        row=1,
        col=1,
    )

    # Overlays
    if "SMA20" in indicators and "SMA_20" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["SMA_20"], name="SMA20", line=dict(color=AMBER, width=1.2), opacity=0.85
            ),
            row=1,
            col=1,
        )
    if "SMA50" in indicators and "SMA_50" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["SMA_50"], name="SMA50", line=dict(color=BLUE, width=1.4), opacity=0.9
            ),
            row=1,
            col=1,
        )
    if "SMA200" in indicators and "SMA_200" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["SMA_200"], name="SMA200", line=dict(color=ORANGE, width=1.6), opacity=0.9
            ),
            row=1,
            col=1,
        )
    if "EMA20" in indicators and "EMA_20" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["EMA_20"], name="EMA20", line=dict(color=PURPLE, width=1.2, dash="dot")
            ),
            row=1,
            col=1,
        )
    if "BB" in indicators and "BB_Upper" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["BB_Upper"],
                name="BB",
                line=dict(color="rgba(100,181,246,0.5)", width=1),
                showlegend=False,
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["BB_Lower"],
                name="BB Lower",
                line=dict(color="rgba(100,181,246,0.5)", width=1),
                fill="tonexty",
                fillcolor="rgba(100,181,246,0.05)",
                showlegend=False,
            ),
            row=1,
            col=1,
        )
    if "VWAP" in indicators and "VWAP" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["VWAP"], name="VWAP", line=dict(color="#00bcd4", width=1.3, dash="dash")
            ),
            row=1,
            col=1,
        )
    if "Supertrend" in indicators and "Supertrend" in df.columns:
        st_dir = df.get("Supertrend_Dir", pd.Series())
        colors = [GREEN if d > 0 else RED for d in st_dir.fillna(0)]
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["Supertrend"],
                name="Supertrend",
                mode="markers+lines",
                marker=dict(color=colors, size=3),
                line=dict(width=1.5),
            ),
            row=1,
            col=1,
        )

    # Latest price annotation
    last = float(df["Close"].iloc[-1])
    color = GREEN if float(df["Close"].iloc[-1]) >= float(df["Open"].iloc[-1]) else RED
    fig.add_annotation(
        x=df.index[-1],
        y=last,
        text=f"  ₹{last:.2f}",
        showarrow=False,
        xanchor="left",
        font=dict(color=color, size=12, family="monospace"),
        row=1,
        col=1,
    )

    # ── Volume ────────────────────────────────────────────────────────────────
    vol_colors = [
        "rgba(38,166,154,0.5)" if float(c) >= float(o) else "rgba(239,83,80,0.5)"
        for c, o in zip(df["Close"], df["Open"], strict=False)
    ]
    fig.add_trace(
        go.Bar(x=df.index, y=df["Volume"], marker_color=vol_colors, name="Vol", showlegend=False),
        row=2,
        col=1,
    )
    if "Volume_SMA20" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["Volume_SMA20"],
                line=dict(color=AMBER, width=1.1),
                name="Vol MA20",
                showlegend=False,
            ),
            row=2,
            col=1,
        )

    # ── RSI ───────────────────────────────────────────────────────────────────
    rsi_row = None
    if show_rsi and "RSI" in df.columns:
        rsi_row = 3
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["RSI"], line=dict(color=PURPLE, width=1.5), name="RSI", showlegend=False
            ),
            row=rsi_row,
            col=1,
        )
        for lvl, _clr in [(70, "rgba(239,83,80,0.3)"), (30, "rgba(38,166,154,0.3)")]:
            fig.add_hline(y=lvl, line_dash="dot", line_color="rgba(255,255,255,0.2)", row=rsi_row, col=1)

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd_row = None
    if show_macd and "MACD" in df.columns:
        macd_row = 3 + int(show_rsi)
        hist_colors = [GREEN if v >= 0 else RED for v in df["MACD_Hist"].fillna(0)]
        fig.add_trace(
            go.Bar(
                x=df.index,
                y=df["MACD_Hist"],
                marker_color=hist_colors,
                name="Hist",
                showlegend=False,
                opacity=0.7,
            ),
            row=macd_row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["MACD"], line=dict(color=BLUE, width=1.3), name="MACD", showlegend=False
            ),
            row=macd_row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["MACD_Signal"],
                line=dict(color=ORANGE, width=1.3),
                name="Signal",
                showlegend=False,
            ),
            row=macd_row,
            col=1,
        )

    # Axis labels
    fig.update_yaxes(title_text="Price (₹)", row=1, col=1, title_font=dict(size=10, color=TEXT))
    fig.update_yaxes(title_text="Volume", row=2, col=1, title_font=dict(size=10, color=TEXT))
    if rsi_row:
        fig.update_yaxes(title_text="RSI", row=rsi_row, col=1, range=[0, 100], title_font=dict(size=10))
    if macd_row:
        fig.update_yaxes(title_text="MACD", row=macd_row, col=1, title_font=dict(size=10))

    fig.update_layout(**_layout(f"{company or symbol} ({symbol}.NS) — {timeframe}"))
    for i in range(1, n_rows + 1):
        fig.update_xaxes(**_axis_style(i))
        fig.update_yaxes(
            showgrid=True,
            gridcolor=GRID,
            gridwidth=0.5,
            zeroline=False,
            tickfont=dict(color=TEXT, size=10),
            row=i,
            col=1,
        )

    return fig


def sector_heatmap(peers: list[dict], sector: str):
    if not peers:
        return None
    syms = [p["symbol"] for p in peers]
    changes = [p["change"] for p in peers]
    prices = [f"₹{p['price']}" for p in peers]
    labels = [f"{s}<br>{c:+.2f}%<br>{pr}" for s, c, pr in zip(syms, changes, prices, strict=False)]
    max_abs = max(abs(c) for c in changes) or 1
    norm = [c / max_abs for c in changes]
    colors = []
    for n in norm:
        if n >= 0:
            r = int(38 + (166 - 38) * (1 - n))
            g = int(166 + (200 - 166) * n)
            b = int(154 - 154 * n)
            colors.append(f"rgb({r},{g},{b})")
        else:
            r = int(239 + (200 - 239) * (-n))
            g = int(83 * (1 + n))
            b = int(80 * (1 + n))
            colors.append(f"rgb({r},{g},{b})")

    fig = go.Figure(
        go.Treemap(
            labels=labels,
            parents=[""] * len(peers),
            values=[abs(c) + 0.5 for c in changes],
            marker=dict(colors=colors, line=dict(width=2, color=BG)),
            textfont=dict(size=12, color="white"),
            hovertemplate="%{label}<extra></extra>",
        )
    )
    fig.update_layout(**_layout(f"{sector} — Peer Heatmap (1D)", height=300))
    return fig


def portfolio_chart(holdings: list[dict]):
    if not holdings:
        return None
    syms = [h["symbol"] for h in holdings]
    values = [h.get("current_value", 0) for h in holdings]
    pnl = [h.get("pnl", 0) for h in holdings]
    pnl_pct = [h.get("pnl_pct", 0) for h in holdings]

    fig = make_subplots(
        rows=1,
        cols=2,
        specs=[[{"type": "domain"}, {"type": "xy"}]],
        subplot_titles=("Allocation", "P&L by Stock"),
    )
    fig.add_trace(
        go.Pie(
            labels=syms,
            values=values,
            hole=0.5,
            marker=dict(line=dict(color=BG, width=2)),
            textfont=dict(size=11),
        ),
        row=1,
        col=1,
    )
    bar_colors = [GREEN if p >= 0 else RED for p in pnl]
    fig.add_trace(
        go.Bar(
            x=syms,
            y=pnl,
            marker_color=bar_colors,
            text=[f"{p:+.1f}%" for p in pnl_pct],
            textposition="outside",
        ),
        row=1,
        col=2,
    )
    fig.update_layout(**_layout("Portfolio Overview", height=340))
    fig.update_xaxes(showgrid=False, row=1, col=2)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, row=1, col=2)
    return fig


def backtest_chart(equity_data: list[float], dates: list, strategy: str, buy_hold: float):
    if not equity_data:
        return None
    norm_eq = [v / equity_data[0] * 100 for v in equity_data]
    bh_line = [100 + (buy_hold / len(equity_data)) * i for i in range(len(equity_data))]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=list(range(len(norm_eq))), y=norm_eq, name=strategy, line=dict(color=BLUE, width=2))
    )
    fig.add_trace(
        go.Scatter(
            x=list(range(len(bh_line))),
            y=bh_line,
            name="Buy & Hold",
            line=dict(color=AMBER, width=1.5, dash="dash"),
        )
    )
    fig.update_layout(**_layout(f"Backtest — {strategy}", height=350))
    fig.update_xaxes(showgrid=True, gridcolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, ticksuffix="%")
    return fig
