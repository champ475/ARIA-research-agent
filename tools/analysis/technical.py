"""
tools/analysis/technical.py
Full TA engine with validated support/resistance, correct R:R, explainability.
"""

import warnings

import numpy as np
import pandas as pd

from config import TA_BB_PERIOD, TA_MACD_FAST, TA_MACD_SIGNAL, TA_MACD_SLOW, TA_RSI_PERIOD

warnings.filterwarnings("ignore", category=RuntimeWarning)

try:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, ADXIndicator
    from ta.volatility import AverageTrueRange, BollingerBands

    TA_OK = True
except ImportError:
    TA_OK = False


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 30:
        return df
    df = df.copy()
    cls = df["Close"].astype(float)
    hi = df["High"].astype(float)
    lo = df["Low"].astype(float)
    vol = df["Volume"].astype(float)

    for n in [9, 20, 50, 100, 200]:
        df[f"SMA_{n}"] = cls.rolling(n).mean()
        df[f"EMA_{n}"] = cls.ewm(span=n, adjust=False).mean()

    if TA_OK:
        bb = BollingerBands(cls, window=TA_BB_PERIOD, window_dev=2)
        df["BB_Upper"] = bb.bollinger_hband()
        df["BB_Mid"] = bb.bollinger_mavg()
        df["BB_Lower"] = bb.bollinger_lband()
        df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"]
        df["RSI"] = RSIIndicator(cls, window=TA_RSI_PERIOD).rsi()
        macd_i = MACD(cls, window_fast=TA_MACD_FAST, window_slow=TA_MACD_SLOW, window_sign=TA_MACD_SIGNAL)
        df["MACD"] = macd_i.macd()
        df["MACD_Signal"] = macd_i.macd_signal()
        df["MACD_Hist"] = macd_i.macd_diff()
        df["ATR"] = AverageTrueRange(hi, lo, cls, window=14).average_true_range()
        st = StochasticOscillator(hi, lo, cls, window=14, smooth_window=3)
        df["Stoch_K"] = st.stoch()
        df["Stoch_D"] = st.stoch_signal()
        adx_i = ADXIndicator(hi, lo, cls, window=14)
        df["ADX"] = adx_i.adx()
        df["ADX_DI+"] = adx_i.adx_pos()
        df["ADX_DI-"] = adx_i.adx_neg()
    else:
        # Manual fallbacks — the same definitions `ta` uses (DATA-03), so the
        # numbers don't change if the library is missing: Bollinger with the
        # population standard deviation, Wilder-smoothed RSI.
        mid = cls.rolling(TA_BB_PERIOD).mean()
        std = cls.rolling(TA_BB_PERIOD).std(ddof=0)
        df["BB_Upper"] = mid + 2 * std
        df["BB_Mid"] = mid
        df["BB_Lower"] = mid - 2 * std
        delta = cls.diff()
        alpha = 1 / TA_RSI_PERIOD
        g = delta.clip(lower=0).ewm(alpha=alpha, min_periods=TA_RSI_PERIOD, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=alpha, min_periods=TA_RSI_PERIOD, adjust=False).mean()
        df["RSI"] = 100 - (100 / (1 + g / loss.replace(0, np.nan)))
        e12 = cls.ewm(span=TA_MACD_FAST, min_periods=TA_MACD_FAST, adjust=False).mean()
        e26 = cls.ewm(span=TA_MACD_SLOW, min_periods=TA_MACD_SLOW, adjust=False).mean()
        df["MACD"] = e12 - e26
        df["MACD_Signal"] = (
            df["MACD"].ewm(span=TA_MACD_SIGNAL, min_periods=TA_MACD_SIGNAL, adjust=False).mean()
        )
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
        tr = pd.concat([hi - lo, (hi - cls.shift()).abs(), (lo - cls.shift()).abs()], axis=1).max(axis=1)
        df["ATR"] = tr.rolling(14).mean()

    # VWAP
    df["VWAP"] = ((hi + lo + cls) / 3 * vol).cumsum() / vol.cumsum()

    # Volume SMA
    df["Volume_SMA20"] = vol.rolling(20).mean()
    df["Volume_Ratio"] = vol / df["Volume_SMA20"]

    # Supertrend
    df = _supertrend(df)

    # Fibonacci (stored as attribute)
    df.attrs["fib"] = _fib_levels(hi, lo)

    return df


def _supertrend(df, period=10, mult=3.0):
    try:
        hi = df["High"].astype(float)
        lo = df["Low"].astype(float)
        cls = df["Close"].astype(float)
        tr = pd.concat([hi - lo, (hi - cls.shift()).abs(), (lo - cls.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        mid = (hi + lo) / 2
        ub = mid + mult * atr
        lb = mid - mult * atr
        st = pd.Series(index=df.index, dtype=float)
        dr = pd.Series(index=df.index, dtype=float)
        for i in range(1, len(df)):
            dr.iloc[i] = (
                1
                if cls.iloc[i] > ub.iloc[i - 1]
                else (-1 if cls.iloc[i] < lb.iloc[i - 1] else dr.iloc[i - 1])
            )
            st.iloc[i] = lb.iloc[i] if dr.iloc[i] == 1 else ub.iloc[i]
        df["Supertrend"] = st
        df["Supertrend_Dir"] = dr
    except Exception:
        pass
    return df


def _fib_levels(hi: pd.Series, lo: pd.Series) -> dict:
    try:
        h52 = float(hi.rolling(252).max().iloc[-1]) if len(hi) >= 252 else float(hi.max())
        l52 = float(lo.rolling(252).min().iloc[-1]) if len(lo) >= 252 else float(lo.min())
        rng = h52 - l52
        return {
            "high": round(h52, 2),
            "low": round(l52, 2),
            "0.236": round(h52 - 0.236 * rng, 2),
            "0.382": round(h52 - 0.382 * rng, 2),
            "0.500": round(h52 - 0.500 * rng, 2),
            "0.618": round(h52 - 0.618 * rng, 2),
            "0.786": round(h52 - 0.786 * rng, 2),
        }
    except Exception:
        return {}


def swing_levels(df: pd.DataFrame, order: int = 5, n_levels: int = 3) -> dict:
    """
    Detect dynamic support/resistance from swing highs and lows.
    order: candles on each side required to qualify as a swing point.
    Returns the nearest n_levels above (resistance) and below (support) current price.
    Pure numpy — no scipy dependency.
    """
    if df.empty or len(df) < order * 2 + 1:
        return {"swing_highs": [], "swing_lows": []}

    hi = df["High"].astype(float).values
    lo = df["Low"].astype(float).values
    price = float(df["Close"].iloc[-1])

    raw_highs, raw_lows = [], []
    for i in range(order, len(hi) - order):
        if hi[i] == hi[i - order : i + order + 1].max():
            raw_highs.append(round(float(hi[i]), 2))  # Python floats: NumPy 2 reprs np.float64(...)
        if lo[i] == lo[i - order : i + order + 1].min():
            raw_lows.append(round(float(lo[i]), 2))

    def _dedup(levels, pct=0.005):
        if not levels:
            return []
        out = [sorted(set(levels))[0]]
        for lvl in sorted(set(levels))[1:]:
            if out and abs(lvl - out[-1]) / max(out[-1], 0.01) > pct:
                out.append(lvl)
        return out

    highs_dedup = _dedup(raw_highs)
    lows_dedup = _dedup(raw_lows)

    resistances = sorted(lvl for lvl in highs_dedup if lvl > price)[:n_levels]
    supports = sorted((lvl for lvl in lows_dedup if lvl < price), reverse=True)[:n_levels]

    return {"swing_highs": resistances, "swing_lows": supports}


def support_resistance(df: pd.DataFrame, window: int = 20) -> dict:
    """
    Pivot-based support/resistance + dynamic swing levels.
    Pivot = (H + L + C) / 3
    R1 = 2*Pivot − Low,  R2 = Pivot + (High − Low)
    S1 = 2*Pivot − High, S2 = Pivot − (High − Low)
    """
    if df.empty or len(df) < window:
        return {}
    last_h = float(df["High"].iloc[-1])
    last_l = float(df["Low"].iloc[-1])
    last_c = float(df["Close"].iloc[-1])

    # 20-day high/low for support/resistance range
    recent = df.tail(window)
    rng_h = float(recent["High"].max())
    rng_l = float(recent["Low"].min())

    pivot = round((last_h + last_l + last_c) / 3, 2)
    r1 = round(2 * pivot - last_l, 2)
    r2 = round(pivot + (last_h - last_l), 2)
    s1 = round(2 * pivot - last_h, 2)
    s2 = round(pivot - (last_h - last_l), 2)

    support = round(rng_l, 2)
    resistance = round(rng_h, 2)

    swings = swing_levels(df)

    return {
        "Pivot": pivot,
        "R1": r1,
        "R2": r2,
        "S1": s1,
        "S2": s2,
        "Support_20": support,
        "Resist_20": resistance,
        "swing_highs": swings["swing_highs"],
        "swing_lows": swings["swing_lows"],
        "formula": (
            f"Pivot = (H+L+C)/3 = ({last_h}+{last_l}+{last_c})/3 = {pivot}\n"
            f"R1 = 2×Pivot − L = 2×{pivot} − {last_l} = {r1}\n"
            f"R2 = Pivot + (H−L) = {pivot} + ({last_h}−{last_l}) = {r2}\n"
            f"S1 = 2×Pivot − H = 2×{pivot} − {last_h} = {s1}\n"
            f"S2 = Pivot − (H−L) = {pivot} − ({last_h}−{last_l}) = {s2}\n"
            f"20D Range: Support={support}, Resistance={resistance}\n"
            f"Swing Resistances: {swings['swing_highs']}\n"
            f"Swing Supports: {swings['swing_lows']}"
        ),
    }


def compute_rr(entry: float, stop: float, target: float, cmp: float = None) -> dict:
    """
    Risk:Reward = (Target − Entry) / (Entry − Stop Loss)
    If CMP is within entry zone, use CMP as entry.
    Validates: stop < entry < target for long trades.
    """
    actual_entry = cmp if cmp and entry * 0.98 <= cmp <= entry * 1.02 else entry

    if not (stop < actual_entry < target):
        return {
            "valid": False,
            "error": f"Invalid setup: stop({stop}) < entry({actual_entry}) < target({target}) not satisfied",
            "rr": None,
            "risk_pts": None,
            "reward_pts": None,
        }

    risk = round(actual_entry - stop, 2)
    reward = round(target - actual_entry, 2)
    rr = round(reward / risk, 2) if risk > 0 else None

    return {
        "valid": True,
        "entry": round(actual_entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_pts": risk,
        "reward_pts": reward,
        "rr": rr,
        "formula": (
            f"R:R = (Target − Entry) / (Entry − Stop Loss)\n"
            f"    = ({target} − {actual_entry}) / ({actual_entry} − {stop})\n"
            f"    = {reward} / {risk}\n"
            f"    = {rr}:1"
            + (f"\n    Note: CMP ₹{cmp} used as entry (within entry zone)" if cmp and cmp != entry else "")
        ),
    }


def detect_patterns(df: pd.DataFrame) -> list[str]:
    patterns = []
    if df.empty or len(df) < 10:
        return patterns

    price = float(df["Close"].iloc[-1])

    def _v(col):
        if col not in df.columns:
            return None
        s = df[col].dropna()
        return float(s.iloc[-1]) if not s.empty else None

    def _v2(col):
        if col not in df.columns:
            return None
        s = df[col].dropna()
        return float(s.iloc[-2]) if len(s) >= 2 else None

    # MA structure
    s50, s200 = _v("SMA_50"), _v("SMA_200")
    if s50 and s200:
        s50p, s200p = _v2("SMA_50"), _v2("SMA_200")
        if s50p and s200p:
            if s50p < s200p and s50 > s200:
                patterns.append("🟢 Golden Cross — SMA50 crossed above SMA200 (strong bullish signal)")
            elif s50p > s200p and s50 < s200:
                patterns.append("🔴 Death Cross — SMA50 crossed below SMA200 (strong bearish signal)")
        if price > s50 > s200:
            patterns.append(f"🟢 Bullish MA stack: Price {price:.2f} > SMA50 {s50:.2f} > SMA200 {s200:.2f}")
        elif price < s50 < s200:
            patterns.append(f"🔴 Bearish MA stack: Price {price:.2f} < SMA50 {s50:.2f} < SMA200 {s200:.2f}")

    # RSI
    rsi = _v("RSI")
    if rsi is not None:
        if rsi > 80:
            patterns.append(f"🔴 RSI Extremely Overbought ({rsi:.1f}) — high reversal risk")
        elif rsi > 70:
            patterns.append(f"🟠 RSI Overbought ({rsi:.1f}) — watch for pullback")
        elif rsi < 20:
            patterns.append(f"🟢 RSI Extremely Oversold ({rsi:.1f}) — potential strong bounce")
        elif rsi < 30:
            patterns.append(f"🟢 RSI Oversold ({rsi:.1f}) — potential reversal zone")
        else:
            patterns.append(f"⚪ RSI Neutral ({rsi:.1f}) — no extreme reading")

    # MACD
    macd, sig, macd_p, sig_p = _v("MACD"), _v("MACD_Signal"), _v2("MACD"), _v2("MACD_Signal")
    if macd is not None and sig is not None and macd_p is not None and sig_p is not None:
        if macd_p < sig_p and macd > sig:
            patterns.append(f"🟢 MACD Bullish Crossover (MACD {macd:.3f} crossed above Signal {sig:.3f})")
        elif macd_p > sig_p and macd < sig:
            patterns.append(f"🔴 MACD Bearish Crossover (MACD {macd:.3f} crossed below Signal {sig:.3f})")
        elif macd > sig:
            patterns.append(f"🟢 MACD above Signal ({macd:.3f} > {sig:.3f}) — bullish momentum")
        else:
            patterns.append(f"🔴 MACD below Signal ({macd:.3f} < {sig:.3f}) — bearish momentum")

    # Supertrend
    st_dir = _v("Supertrend_Dir")
    st_val = _v("Supertrend")
    if st_dir is not None and st_val is not None:
        patterns.append(
            f"{'🟢 Supertrend BULLISH' if st_dir > 0 else '🔴 Supertrend BEARISH'} (level ₹{st_val:.2f})"
        )

    # Bollinger
    bb_u, bb_l = _v("BB_Upper"), _v("BB_Lower")
    if bb_u and bb_l:
        if price > bb_u:
            patterns.append(f"🔴 Price ₹{price:.2f} above Upper BB ₹{bb_u:.2f} — overbought/breakout")
        elif price < bb_l:
            patterns.append(f"🟢 Price ₹{price:.2f} below Lower BB ₹{bb_l:.2f} — oversold/reversal")

    # Volume
    vr = _v("Volume_Ratio")
    if vr and vr > 2.5:
        patterns.append(f"📈 Volume {vr:.1f}× average — significant interest, confirm direction")

    # VWAP
    vwap = _v("VWAP")
    if vwap:
        patterns.append(
            f"{'🟢 Above VWAP' if price >= vwap else '🔴 Below VWAP'} (₹{price:.2f} vs VWAP ₹{vwap:.2f})"
        )

    # ADX
    adx = _v("ADX")
    if adx:
        if adx > 40:
            patterns.append(f"📊 Very Strong Trend (ADX {adx:.1f}) — momentum driven")
        elif adx > 25:
            patterns.append(f"📊 Trending Market (ADX {adx:.1f}) — trend is intact")
        else:
            patterns.append(f"📊 Weak/No Trend (ADX {adx:.1f}) — range-bound")

    return patterns


def snapshot(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}

    def _s(col):
        if col not in df.columns:
            return None
        v = df[col].dropna()
        if v.empty:
            return None
        f = float(v.iloc[-1])
        return None if (f != f) else round(f, 4)

    return {
        "current_price": _s("Close"),
        "open": _s("Open"),
        "high": _s("High"),
        "low": _s("Low"),
        "SMA_9": _s("SMA_9"),
        "EMA_9": _s("EMA_9"),
        "SMA_20": _s("SMA_20"),
        "EMA_20": _s("EMA_20"),
        "SMA_50": _s("SMA_50"),
        "EMA_50": _s("EMA_50"),
        "SMA_100": _s("SMA_100"),
        "SMA_200": _s("SMA_200"),
        "EMA_200": _s("EMA_200"),
        "RSI": _s("RSI"),
        "MACD": _s("MACD"),
        "MACD_Signal": _s("MACD_Signal"),
        "MACD_Hist": _s("MACD_Hist"),
        "BB_Upper": _s("BB_Upper"),
        "BB_Mid": _s("BB_Mid"),
        "BB_Lower": _s("BB_Lower"),
        "BB_Width": _s("BB_Width"),
        "ATR": _s("ATR"),
        "Stoch_K": _s("Stoch_K"),
        "Stoch_D": _s("Stoch_D"),
        "ADX": _s("ADX"),
        "ADX_DI+": _s("ADX_DI+"),
        "ADX_DI-": _s("ADX_DI-"),
        "VWAP": _s("VWAP"),
        "Supertrend": _s("Supertrend"),
        "Supertrend_Dir": _s("Supertrend_Dir"),
        "Volume": _s("Volume"),
        "Volume_SMA20": _s("Volume_SMA20"),
        "Volume_Ratio": _s("Volume_Ratio"),
        "fib": df.attrs.get("fib", {}),
        "patterns": detect_patterns(df),
        "swings": swing_levels(df),
    }


def indicator_breakdown(ta: dict) -> str:
    """
    Full explainability section: every indicator with value + interpretation.
    """

    def _row(name, val, interp):
        v = f"{val:.2f}" if isinstance(val, float) else (str(val) if val is not None else "N/A")
        return f"| {name} | {v} | {interp} |"

    price = ta.get("current_price")
    rsi = ta.get("RSI")
    macd = ta.get("MACD")
    sig = ta.get("MACD_Signal")
    hist = ta.get("MACD_Hist")
    adx = ta.get("ADX")
    atr = ta.get("ATR")
    bb_u = ta.get("BB_Upper")
    bb_l = ta.get("BB_Lower")
    vr = ta.get("Volume_Ratio")
    st_dir = ta.get("Supertrend_Dir")

    def _rsi_interp(v):
        if v is None:
            return "N/A"
        if v > 80:
            return "Extremely overbought — high reversal risk"
        if v > 70:
            return "Overbought — watch for pullback"
        if v > 60:
            return "Bullish momentum"
        if v > 40:
            return "Neutral zone"
        if v > 30:
            return "Bearish momentum"
        if v > 20:
            return "Oversold — potential bounce"
        return "Extremely oversold — high bounce risk"

    def _adx_interp(v):
        if v is None:
            return "N/A"
        if v > 50:
            return "Extremely strong trend"
        if v > 40:
            return "Very strong trend"
        if v > 25:
            return "Trending — trade in direction"
        if v > 20:
            return "Weakening trend"
        return "No trend / range-bound"

    def _atr_interp(v):
        if v is None or price is None or price == 0:
            return "N/A"
        pct = v / price * 100
        if pct > 5:
            return f"{pct:.1f}% of price — very high volatility"
        if pct > 3:
            return f"{pct:.1f}% of price — elevated volatility"
        return f"{pct:.1f}% of price — moderate volatility"

    lines = [
        "## 📊 Indicator Breakdown",
        "",
        "| Indicator | Value | Interpretation |",
        "|-----------|-------|----------------|",
        _row("CMP", price, "Current Market Price"),
        _row("RSI (14)", rsi, _rsi_interp(rsi)),
        _row("MACD", macd, f"{'Bullish' if macd and sig and macd > sig else 'Bearish'} vs Signal {sig}"),
        _row("MACD Signal", sig, "9-period EMA of MACD"),
        _row(
            "MACD Histogram",
            hist,
            f"{'Positive — expanding bullish momentum' if hist and hist > 0 else 'Negative — bearish momentum'}",
        ),
        _row(
            "SMA 20",
            ta.get("SMA_20"),
            f"{'Price above' if price and ta.get('SMA_20') and price > ta.get('SMA_20') else 'Price below'} short-term MA",
        ),
        _row(
            "SMA 50",
            ta.get("SMA_50"),
            f"{'Price above' if price and ta.get('SMA_50') and price > ta.get('SMA_50') else 'Price below'} medium-term MA",
        ),
        _row(
            "SMA 200",
            ta.get("SMA_200"),
            f"{'Price above' if price and ta.get('SMA_200') and price > ta.get('SMA_200') else 'Price below'} long-term MA",
        ),
        _row("EMA 20", ta.get("EMA_20"), "Fast exponential MA"),
        _row(
            "BB Upper",
            bb_u,
            f"{'Price at/above upper band — overbought' if price and bb_u and price >= bb_u else 'Price inside bands'}",
        ),
        _row(
            "BB Lower",
            bb_l,
            f"{'Price at/below lower band — oversold' if price and bb_l and price <= bb_l else 'Price inside bands'}",
        ),
        _row("BB Width", ta.get("BB_Width"), "Volatility squeeze indicator"),
        _row("ATR (14)", atr, _atr_interp(atr)),
        _row("ADX", adx, _adx_interp(adx)),
        _row("ADX DI+", ta.get("ADX_DI+"), "Positive directional indicator"),
        _row("ADX DI-", ta.get("ADX_DI-"), "Negative directional indicator"),
        _row("Stoch K", ta.get("Stoch_K"), "Fast stochastic"),
        _row("Stoch D", ta.get("Stoch_D"), "Slow stochastic"),
        _row(
            "VWAP",
            ta.get("VWAP"),
            f"{'Price above VWAP — bullish intraday' if price and ta.get('VWAP') and price > ta.get('VWAP') else 'Price below VWAP — bearish intraday'}",
        ),
        _row(
            "Supertrend",
            ta.get("Supertrend"),
            f"{'BULLISH trend confirmed' if st_dir and st_dir > 0 else 'BEARISH trend confirmed'}",
        ),
        _row(
            "Volume Ratio",
            vr,
            f"{'Above average — significant activity' if vr and vr > 1.5 else 'Below/normal average volume'}",
        ),
    ]

    fib = ta.get("fib", {})
    if fib:
        lines += [
            "",
            "### Fibonacci Retracement Levels (52-week range)",
            "| Level | Price |",
            "|-------|-------|",
            f"| 52W High | ₹{fib.get('high', 'N/A')} |",
            f"| 78.6% retracement | ₹{fib.get('0.786', 'N/A')} |",
            f"| 61.8% retracement | ₹{fib.get('0.618', 'N/A')} |",
            f"| 50.0% retracement | ₹{fib.get('0.500', 'N/A')} |",
            f"| 38.2% retracement | ₹{fib.get('0.382', 'N/A')} |",
            f"| 23.6% retracement | ₹{fib.get('0.236', 'N/A')} |",
            f"| 52W Low | ₹{fib.get('low', 'N/A')} |",
        ]

    return "\n".join(lines)
