"""
scripts/check_alerts.py — standalone alert evaluator.

Run on a schedule by .github/workflows/alert-check.yml (GitHub Actions cron,
every 5 min). Not part of the Gradio app process — uses a single direct
psycopg2 connection since this is a short-lived process that opens one
connection and exits (the app's ThreadedConnectionPool in utils/db.py would
be pointless here); this is a deliberate, still-valid choice, not something
the multi-user Telegram routing below needed to change.

Multi-user Telegram routing: each alert's notification goes to ITS OWNER's
own linked Telegram chat (users.telegram_chat_id, set via the in-app
"Connect Telegram" flow — utils/telegram_link.py), not a single shared
TELEGRAM_CHAT_ID. The bot token stays a single shared value (one bot serves
every user's chat, Telegram's API is chat_id-addressed already) — only the
per-alert destination changed.

Delivery contract (DATA-08): an alert is marked triggered (one-shot alerts
deactivated, recurring ones start their cooldown) only once its message has
been delivered somewhere the user will see it:
- Telegram linked and the send confirmed (HTTP 200 with "ok": true), or
- written to the user's in-app inbox (the `notifications` table, shown in the
  Settings tab) — used when the user hasn't linked Telegram, the bot isn't
  configured, or Telegram has failed ALERT_MAX_DELIVERY_ATTEMPTS runs in a row.
A transient Telegram failure leaves the alert untouched apart from a
delivery_attempts counter, so the next run tries again.

Market data is fetched once per symbol per run (RunCache), not once per alert.

Usage: python scripts/check_alerts.py
Requires env vars: DATABASE_URL, TELEGRAM_BOT_TOKEN.
"""

import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL, TELEGRAM_BOT_TOKEN
from tools.analysis.technical import compute_all
from tools.data.fetcher import get_info, get_ohlcv
from tools.data.nse_scraper import get_bulk_deals
from utils.db import ALERT_DELIVERY_SCHEMA_SQL
from utils.helpers import IST
from utils.logging_setup import configure_logging

log = logging.getLogger("check_alerts")

ALERT_MAX_DELIVERY_ATTEMPTS = int(os.getenv("ALERT_MAX_DELIVERY_ATTEMPTS", "5"))

# Failures that retrying can't fix — go straight to the in-app inbox.
_PERMANENT_DELIVERY_ERRORS = {"not_linked", "bot_not_configured"}


def market_is_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def send_telegram(text: str, chat_id: str | None) -> tuple[bool, str | None]:
    """Returns (delivered, error_code). Delivered only on HTTP 200 with
    Telegram's own `"ok": true` — a 200 carrying `"ok": false` is a failure."""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram bot not configured — cannot send notification")
        return False, "bot_not_configured"
    if not chat_id:
        return False, "not_linked"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # No parse_mode — alert text (symbols, alert_type names) can contain
        # underscores/asterisks that Telegram's Markdown parser rejects as
        # malformed entities (e.g. "price_below" -> 400 Bad Request).
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False, f"request_failed:{type(e).__name__}"
    if r.status_code != 200:
        log.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
        return False, f"http_{r.status_code}"
    try:
        ok = r.json().get("ok") is True
    except ValueError:
        ok = False
    if not ok:
        log.warning(f"Telegram send returned ok=false: {r.text[:200]}")
        return False, "telegram_not_ok"
    return True, None


class RunCache:
    """Memoises market-data fetches for one checker run, so N alerts on the
    same symbol cost one fetch. Failures are memoised too (re-raised for every
    alert that needs that data) so a broken symbol isn't retried N times."""

    def __init__(self) -> None:
        self._memo: dict[tuple, Any] = {}

    def _get(self, key: tuple, fetch: Callable[[], Any]) -> Any:
        if key not in self._memo:
            try:
                self._memo[key] = (True, fetch())
            except Exception as e:
                self._memo[key] = (False, e)
        ok, value = self._memo[key]
        if not ok:
            raise value
        return value

    def info(self, symbol: str) -> dict:
        return self._get(("info", symbol), lambda: get_info(symbol))

    def ohlcv(self, symbol: str, period: str, interval: str):
        return self._get(
            ("ohlcv", symbol, period, interval), lambda: get_ohlcv(symbol, period=period, interval=interval)
        )

    def indicators(self, symbol: str):
        def build():
            df = self.ohlcv(symbol, "1y", "1d")
            if df is None or len(df) < 30:
                return None
            return compute_all(df.copy())

        return self._get(("indicators", symbol), build)

    def bulk_deals(self, symbol: str):
        return self._get(("bulk", symbol), lambda: get_bulk_deals(symbol))


def _price_alert(cache: RunCache, symbol: str, alert_type: str, threshold: float) -> str | None:
    info = cache.info(symbol)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None or threshold is None:
        return None
    if alert_type == "price_above" and price > threshold:
        return f"🔔 {symbol} is now ₹{price:.2f} — above your alert threshold of ₹{threshold:.2f}"
    if alert_type == "price_below" and price < threshold:
        return f"🔔 {symbol} is now ₹{price:.2f} — below your alert threshold of ₹{threshold:.2f}"
    return None


def _pct_change_alert(cache: RunCache, symbol: str, threshold: float) -> str | None:
    df = cache.ohlcv(symbol, "5d", "1d")
    if df is None or len(df) < 2 or threshold is None:
        return None
    pct = (df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100
    if abs(pct) >= abs(threshold):
        return f"🔔 {symbol} moved {pct:+.2f}% — beyond your {threshold}% alert"
    return None


def _indicator_alert(cache: RunCache, symbol: str, alert_type: str, threshold: float | None) -> str | None:
    df = cache.indicators(symbol)
    if df is None or len(df) < 2:
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


def _bulk_deal_alert(cache: RunCache, symbol: str) -> str | None:
    deals = cache.bulk_deals(symbol)
    if deals is not None and not deals.empty:
        return f"🔔 Bulk deal detected for {symbol} today"
    return None


def _portfolio_pnl_alert(
    conn, cache: RunCache, user_id: int, symbol: str, threshold: float | None
) -> str | None:
    if threshold is None:
        return None
    cur = conn.cursor()
    cur.execute("SELECT qty, avg_price FROM portfolio WHERE user_id=%s AND symbol=%s", (user_id, symbol))
    rows = cur.fetchall()
    if not rows:
        return None
    info = cache.info(symbol)
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
    "rsi_cross_above",
    "rsi_cross_below",
    "macd_bullish_cross",
    "macd_bearish_cross",
    "ma_cross",
    "bb_breakout",
    "volume_spike",
    "week52_high",
    "week52_low",
}


def in_cooldown(alert: dict) -> bool:
    """Recurring alerts that already fired must wait out cooldown_hours before
    re-firing, or a condition that stays true (e.g. price_above once crossed)
    would spam a notification every 5-min cron tick."""
    if not alert.get("recurring") or alert.get("triggered_at") is None:
        return False
    cooldown = alert.get("cooldown_hours") or 24
    triggered_at = alert["triggered_at"]
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - triggered_at < timedelta(hours=cooldown)


def evaluate(conn, alert: dict, cache: RunCache | None = None) -> str | None:
    cache = cache or RunCache()
    symbol, atype, threshold = alert["symbol"], alert["alert_type"], alert["threshold"]
    if atype in ("price_above", "price_below"):
        return _price_alert(cache, symbol, atype, threshold)
    if atype == "pct_change":
        return _pct_change_alert(cache, symbol, threshold)
    if atype in _INDICATOR_TYPES:
        return _indicator_alert(cache, symbol, atype, threshold)
    if atype == "bulk_deal":
        return _bulk_deal_alert(cache, symbol)
    if atype == "portfolio_pnl":
        return _portfolio_pnl_alert(conn, cache, alert["user_id"], symbol, threshold)
    return None


def _mark_triggered(cur, alert: dict, channel: str) -> None:
    deactivate = "" if alert.get("recurring") else "active=FALSE, "
    # `deactivate` is one of two constant fragments, never user input.
    cur.execute(
        f"UPDATE alerts SET {deactivate}triggered_at=now(), delivery_status=%s, "  # noqa: S608  # nosec B608
        "delivery_attempts=0, last_delivery_error=NULL WHERE id=%s",
        (channel, alert["id"]),
    )


def deliver(cur, alert: dict, msg: str) -> str:
    """Deliver one fired alert and record the outcome in the same transaction.
    Returns "telegram", "in_app" or "retry"."""
    ok, err = send_telegram(msg, alert.get("telegram_chat_id"))
    if ok:
        _mark_triggered(cur, alert, "telegram")
        return "telegram"
    attempts = (alert.get("delivery_attempts") or 0) + 1
    if err in _PERMANENT_DELIVERY_ERRORS or attempts >= ALERT_MAX_DELIVERY_ATTEMPTS:
        cur.execute(
            "INSERT INTO notifications (user_id, alert_id, message) VALUES (%s, %s, %s)",
            (alert["user_id"], alert["id"], msg),
        )
        _mark_triggered(cur, alert, "in_app")
        return "in_app"
    cur.execute(
        "UPDATE alerts SET delivery_attempts=%s, last_delivery_error=%s, delivery_status='retrying' WHERE id=%s",
        (attempts, err, alert["id"]),
    )
    return "retry"


def ensure_alert_delivery_schema(conn) -> None:
    cur = conn.cursor()
    cur.execute(ALERT_DELIVERY_SCHEMA_SQL)
    conn.commit()


def run_once(conn) -> dict[str, int]:
    """Evaluate every active alert once. Returns outcome counts."""
    ensure_alert_delivery_schema(conn)
    cur = conn.cursor()
    # JOIN pulls each alert owner's linked Telegram chat_id in the same
    # query — every alert notifies ITS OWNER's own chat, not a single
    # shared one (see module docstring).
    cur.execute(
        "SELECT alerts.*, users.telegram_chat_id FROM alerts "
        "JOIN users ON alerts.user_id = users.id "
        "WHERE alerts.active=TRUE ORDER BY alerts.symbol, alerts.id"
    )
    alerts = cur.fetchall()
    log.info(f"Evaluating {len(alerts)} active alert(s).")
    cache = RunCache()
    counts = {"fired": 0, "telegram": 0, "in_app": 0, "retry": 0, "errors": 0}
    for alert in alerts:
        alert = dict(alert)
        if in_cooldown(alert):
            continue
        try:
            msg = evaluate(conn, alert, cache)
        except Exception as e:
            counts["errors"] += 1
            log.exception(f"Error evaluating alert {alert['id']} ({alert['symbol']}): {e}")
            continue
        if not msg:
            continue
        counts["fired"] += 1
        try:
            outcome = deliver(cur, alert, msg)
            conn.commit()
        except Exception as e:
            conn.rollback()
            counts["errors"] += 1
            log.exception(f"Error recording delivery for alert {alert['id']}: {e}")
            continue
        counts[outcome] += 1
        log.info(f"Fired alert {alert['id']} via {outcome}: {msg}")
    log.info(f"Run summary: {counts}")
    return counts


def main():
    if not market_is_open():
        log.info("Market closed — skipping.")
        return
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        run_once(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    configure_logging()
    main()
