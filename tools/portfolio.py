"""tools/portfolio.py — persistent portfolio via SQLite"""

import yfinance as yf
from utils.db import portfolio_add, portfolio_remove, portfolio_all
from config import NSE_SUFFIX


def add(user_id: int, symbol: str, qty: float, avg_price: float) -> str:
    portfolio_add(user_id, symbol, qty, avg_price)
    return f"✅ Added {qty} shares of **{symbol.upper()}** @ ₹{avg_price:.2f} to portfolio."


def remove(user_id: int, symbol: str) -> str:
    portfolio_remove(user_id, symbol)
    return f"✅ Removed **{symbol.upper()}** from portfolio."


def get_live(user_id: int) -> tuple[list[dict], dict]:
    rows   = portfolio_all(user_id)
    result = []
    total_inv = total_val = 0.0

    for r in rows:
        sym  = r["symbol"]
        qty  = r["qty"]
        avg  = r["avg_price"]
        try:
            info  = yf.Ticker(f"{sym}{NSE_SUFFIX}").info
            price = float(info.get("currentPrice") or info.get("regularMarketPrice") or avg)
        except Exception:
            price = avg

        inv     = qty * avg
        val     = qty * price
        pnl     = val - inv
        pnl_pct = (pnl / inv * 100) if inv else 0.0
        total_inv += inv
        total_val += val

        result.append({
            "symbol"       : sym, "qty": qty, "avg_price": avg,
            "current_price": round(price, 2), "current_value": round(val, 2),
            "invested"     : round(inv, 2), "pnl": round(pnl, 2),
            "pnl_pct"      : round(pnl_pct, 2),
        })

    total_pnl     = total_val - total_inv
    total_pnl_pct = (total_pnl / total_inv * 100) if total_inv else 0.0

    summary = {
        "total_invested": round(total_inv, 2),
        "total_value"   : round(total_val, 2),
        "total_pnl"     : round(total_pnl, 2),
        "total_pnl_pct" : round(total_pnl_pct, 2),
        "n_holdings"    : len(result),
    }
    return result, summary


def to_markdown(holdings: list[dict], summary: dict) -> str:
    if not holdings:
        return (
            "Portfolio is empty.\n\n"
            "**Add holdings:** `add TCS 10 shares at 3500`"
        )
    sign = "🟢" if summary["total_pnl"] >= 0 else "🔴"
    lines = [
        "## 📊 Portfolio",
        f"| Invested | Value | P&L | Return |",
        f"|----------|-------|-----|--------|",
        f"| ₹{summary['total_invested']:,.0f} | ₹{summary['total_value']:,.0f} | "
        f"{sign} ₹{summary['total_pnl']:+,.0f} | {summary['total_pnl_pct']:+.2f}% |",
        "",
        "| Symbol | Qty | Avg | LTP | P&L | % |",
        "|--------|-----|-----|-----|-----|---|",
    ]
    for h in sorted(holdings, key=lambda x: x["current_value"], reverse=True):
        s = "🟢" if h["pnl"] >= 0 else "🔴"
        lines.append(
            f"| **{h['symbol']}** | {h['qty']} | ₹{h['avg_price']:,.2f} | "
            f"₹{h['current_price']:,.2f} | {s} ₹{h['pnl']:+,.0f} | {h['pnl_pct']:+.2f}% |"
        )
    return "\n".join(lines)
