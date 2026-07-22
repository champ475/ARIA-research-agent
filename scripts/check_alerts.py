"""
scripts/check_alerts.py — standalone alert evaluator.

Run on a schedule by .github/workflows/alert-check.yml (GitHub Actions cron,
every 5 min). Not part of the Gradio app process — uses a single direct
psycopg2 connection since this is a short-lived process that opens one
connection and exits (the app's ThreadedConnectionPool in utils/db.py would
be pointless here).

Usage: python scripts/check_alerts.py
Requires env vars: DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from utils.helpers import IST
from tools.data.fetcher import get_info, get_ohlcv
from tools.analysis.technical import compute_all
from tools.data.nse_scraper import get_bulk_deals


def market_is_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured — skipping notification:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # No parse_mode — alert text (symbols, alert_type names) can contain
        # underscores/asterisks that Telegram's Markdown parser rejects as
        # malformed entities (e.g. "price_below" -> 400 Bad Request).
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")


def _price_alert(symbol: str, alert_type: str, threshold: float) -> str | None:
    info = get_info(symbol)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None or threshold is None:
        return None
    if alert_type == "price_above" and price > threshold:
        return f"🔔 {symbol} is now ₹{price:.2f} — above your alert threshold of ₹{threshold:.2f}"
    if alert_type == "price_below" and price < threshold:
        return f"🔔 {symbol} is now ₹{price:.2f} — below your alert threshold of ₹{threshold:.2f}"
    return None


def _pct_change_alert(symbol: str, threshold: float) -> str | None:
    df = get_ohlcv(symbol, period="5d", interval="1d")
    if df is None or len(df) < 2 or threshold is None:
        return None
    pct = (df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100
    if abs(pct) >= abs(threshold):
        return f"🔔 {symbol} moved {pct:+.2f}% — beyond your {threshold}% alert"
    return None


def _indicator_alert(symbol: str, alert_type: str, threshold: float | None) -> str | None:
    df = get_ohlcv(symbol, period="1y", interval="1d")
    if df is None or len(df) < 30:
        return None
    df = compute_all(df)
    if len(df) < 2:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]

    if alert_type == "rsi_cross_above" and threshold is not None:
        if prev["RSI"] <= threshold < last["RSI"]:
            return f"🔔 {symbol} RSI crossed above {threshold} (now {last['RSI']:.1f})"
        return None
    if alert_type == "rsi_cross_below" and threshold is not None:
        if prev["RSI"] >= threshold > last["RSI"]:
            return f"🔔 {symbol} RSI crossed below {threshold} (now {last['RSI']:.1f})"
        return None
    if alert_type == "macd_bullish_cross":
        if prev["MACD"] <= prev["MACD_Signal"] and last["MACD"] > last["MACD_Signal"]:
            return f"🔔 {symbol} MACD bullish crossover"
        return None
    if alert_type == "macd_bearish_cross":
        if prev["MACD"] >= prev["MACD_Signal"] and last["MACD"] < last["MACD_Signal"]:
            return f"🔔 {symbol} MACD bearish crossover"
        return None
    if alert_type == "ma_cross":
        if prev["SMA_50"] <= prev["SMA_200"] and last["SMA_50"] > last["SMA_200"]:
            return f"🔔 {symbol} golden cross (50-DMA crossed above 200-DMA)"
        if prev["SMA_50"] >= prev["SMA_200"] and last["SMA_50"] < last["SMA_200"]:
            return f"🔔 {symbol} death cross (50-DMA crossed below 200-DMA)"
        return None
    if alert_type == "bb_breakout":
        if last["Close"] > last["BB_Upper"]:
            return f"🔔 {symbol} broke above its upper Bollinger Band (₹{last['Close']:.2f})"
        if last["Close"] < last["BB_Lower"]:
            return f"🔔 {symbol} broke below its lower Bollinger Band (₹{last['Close']:.2f})"
        return None
    if alert_type == "volume_spike":
        vt = threshold or 2.0
        if last["Volume_Ratio"] is not None and last["Volume_Ratio"] >= vt:
            return f"🔔 {symbol} volume spike — {last['Volume_Ratio']:.1f}x average"
        return None
    if alert_type == "week52_high":
        fib = df.attrs.get("fib", {})
        if fib and last["Close"] >= fib.get("high", float("inf")):
            return f"🔔 {symbol} hit a new 52-week high (₹{last['Close']:.2f})"
        return None
    if alert_type == "week52_low":
        fib = df.attrs.get("fib", {})
        if fib and last["Close"] <= fib.get("low", float("-inf")):
            return f"🔔 {symbol} hit a new 52-week low (₹{last['Close']:.2f})"
        return None
    return None


def _bulk_deal_alert(symbol: str) -> str | None:
    deals = get_bulk_deals.func(symbol)
    if deals is not None and not deals.empty:
        return f"🔔 Bulk deal detected for {symbol} today"
    return None


def _portfolio_pnl_alert(conn, user_id: int, symbol: str, threshold: float | None) -> str | None:
    if threshold is None:
        return None
    cur = conn.cursor()
    cur.execute("SELECT qty, avg_price FROM portfolio WHERE user_id=%s AND symbol=%s", (user_id, symbol))
    rows = cur.fetchall()
    if not rows:
        return None
    info = get_info(symbol)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None:
        return None
    qty = sum(r["qty"] for r in rows)
    cost = sum(r["qty"] * r["avg_price"] for r in rows)
    if cost == 0:
        return None
    pnl_pct = (qty * price - cost) / cost * 100
    if abs(pnl_pct) >= abs(threshold):
        return f"🔔 {symbol} portfolio P&L is {pnl_pct:+.2f}% — beyond your {threshold}% alert"
    return None


_INDICATOR_TYPES = {
    "rsi_cross_above", "rsi_cross_below", "macd_bullish_cross", "macd_bearish_cross",
    "ma_cross", "bb_breakout", "volume_spike", "week52_high", "week52_low",
}


def evaluate(conn, alert: dict) -> str | None:
    symbol, atype, threshold = alert["symbol"], alert["alert_type"], alert["threshold"]
    if atype in ("price_above", "price_below"):
        return _price_alert(symbol, atype, threshold)
    if atype == "pct_change":
        return _pct_change_alert(symbol, threshold)
    if atype in _INDICATOR_TYPES:
        return _indicator_alert(symbol, atype, threshold)
    if atype == "bulk_deal":
        return _bulk_deal_alert(symbol)
    if atype == "portfolio_pnl":
        return _portfolio_pnl_alert(conn, alert["user_id"], symbol, threshold)
    return None


def main():
    if not market_is_open():
        print("Market closed — skipping.")
        return

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM alerts WHERE active=TRUE ORDER BY id")
        alerts = cur.fetchall()
        print(f"Evaluating {len(alerts)} active alert(s).")
        for alert in alerts:
            try:
                msg = evaluate(conn, dict(alert))
            except Exception as e:
                print(f"Error evaluating alert {alert['id']} ({alert['symbol']}): {e}")
                continue
            if msg:
                send_telegram(msg)
                cur.execute(
                    "UPDATE alerts SET active=FALSE, triggered_at=now() WHERE id=%s",
                    (alert["id"],),
                )
                conn.commit()
                print(f"Fired alert {alert['id']}: {msg}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
