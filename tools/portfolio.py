"""
tools/portfolio.py — holdings, live P&L and broker CSV import (UX-05).

Each `portfolio` row is one lot (symbol, qty, avg price, optional buy date);
a holding is a symbol's lots added up. Money and quantities are Decimal end
to end (NUMERIC in Postgres) — floats appear only for display and charts.

Used by the Portfolio tab (app.py), the `portfolio_view` chat tool and the
`portfolio_pnl` alert (scripts/check_alerts.py).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from utils.errors import UserInputError
from utils.escaping import md_cell

MAX_QTY = Decimal("1000000000")
MAX_PRICE = Decimal("10000000")
MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 500
_CENTS = Decimal("0.01")


def _dec(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


# ── input ────────────────────────────────────────────────────────────────────


def parse_quantity(raw: Any) -> Decimal:
    from utils.numbers import parse_number

    p = parse_number(raw if not isinstance(raw, str) else raw.strip())
    if p is None or p.percent or p.currency:
        raise UserInputError("Enter the number of shares, e.g. 10.")
    q = p.value
    if q <= 0 or q > MAX_QTY:
        raise UserInputError("Quantity must be more than 0.")
    if q != q.quantize(Decimal("0.0001")):
        raise UserInputError("Quantity can have at most 4 decimal places.")
    return q


def parse_price(raw: Any) -> Decimal:
    from utils.numbers import parse_inr_amount

    p = parse_inr_amount(raw if not isinstance(raw, str) else raw.strip())
    if p is None:
        raise UserInputError("Enter the average buy price in rupees, e.g. 3,450.50.")
    if p.value < 0 or p.value > MAX_PRICE:
        raise UserInputError("The average price looks wrong — enter the per-share price in rupees.")
    return p.value.quantize(Decimal("0.0001"))


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y")


def parse_buy_date(raw: Any) -> date | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, datetime):
        d = raw.date()
    elif isinstance(raw, date):
        d = raw
    else:
        text = str(raw).strip()[:20]
        for fmt in _DATE_FORMATS:
            try:
                d = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        else:
            raise UserInputError("Enter the buy date as YYYY-MM-DD or DD-MM-YYYY, or leave it blank.")
    if d > date.today() or d.year < 1990:
        raise UserInputError("The buy date must be in the past.")
    return d


def parse_lot(symbol: Any, qty: Any, price: Any, buy_date: Any = None) -> dict[str, Any]:
    """One validated lot, or UserInputError saying what to fix."""
    from utils.forms import watchlist_symbol

    return {
        "symbol": watchlist_symbol(str(symbol or "")),
        "qty": parse_quantity(qty),
        "avg_price": parse_price(price),
        "buy_date": parse_buy_date(buy_date),
    }


# ── CSV import ───────────────────────────────────────────────────────────────

# Normalised header -> field. Covers Zerodha Console holdings ("Instrument",
# "Qty.", "Avg. cost"), Groww ("Stock Name"/"ISIN", "Quantity", "Average buy
# price") and plain sheets ("symbol", "quantity", "avg price", "buy date").
_HEADERS = {
    "symbol": {"symbol", "instrument", "tradingsymbol", "trading symbol", "stock", "stock symbol", "scrip"},
    "name": {"stock name", "company", "company name", "name", "security name"},
    "isin": {"isin", "isin code"},
    "qty": {
        "qty",
        "quantity",
        "shares",
        "quantity available",
        "net qty",
        "holding quantity",
        "qty available",
    },
    "avg_price": {
        "avg cost",
        "average cost",
        "avg price",
        "average price",
        "average buy price",
        "avg buy price",
        "buy price",
        "buy avg",
        "cost price",
        "avg trading price",
    },
    "buy_date": {"date", "buy date", "trade date", "purchase date"},
}


def _norm_header(h: str) -> str:
    return " ".join(re.sub(r"[._()]", " ", (h or "").lower()).split())


def _columns(header: list[str]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        n = _norm_header(h)
        for field, names in _HEADERS.items():
            if n in names and field not in cols:
                cols[field] = i
    return cols


def _symbol_from(row: list[str], cols: dict[str, int]) -> str:
    from tools.data.instruments import master
    from utils.forms import watchlist_symbol

    if "isin" in cols:
        isin = row[cols["isin"]].strip().upper()
        inst = master().by_isin.get(isin)
        if inst is not None:
            return inst.symbol
    for field in ("symbol", "name"):
        if field in cols and row[cols[field]].strip():
            # Zerodha marks some instruments "SYMBOL-BE" / "SYMBOL-E": keep the symbol.
            return watchlist_symbol(re.sub(r"-(?:BE|BZ|E|SM|ST)$", "", row[cols[field]].strip()))
    raise UserInputError("no symbol")


def parse_csv(content: bytes | str) -> tuple[list[dict[str, Any]], list[str]]:
    """(lots, problems) from a broker holdings export or a simple sheet. A
    title block above the header row (Groww) is skipped; blank and total rows
    are ignored; each unusable row is reported, the rest still import."""
    if isinstance(content, bytes):
        if len(content) > MAX_CSV_BYTES:
            raise UserInputError("That file is too large — holdings exports are well under 1 MB.")
        content = content.decode("utf-8-sig", errors="replace")
    try:
        dialect: Any = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(content), dialect))
    header_at = next(
        (
            i
            for i, r in enumerate(rows[:30])
            if {"qty", "avg_price"} <= set(_columns(r)) and len(_columns(r)) >= 3
        ),
        None,
    )
    if header_at is None:
        raise UserInputError(
            "Couldn't find the columns in that file. It needs a symbol (or stock name / ISIN), "
            "a quantity and an average price column."
        )
    cols = _columns(rows[header_at])
    lots: list[dict[str, Any]] = []
    problems: list[str] = []
    for n, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        if not any(c.strip() for c in row) or (row and row[0].strip().lower() in {"total", "grand total"}):
            continue
        if len(lots) >= MAX_CSV_ROWS:
            problems.append(f"Stopped after {MAX_CSV_ROWS} holdings.")
            break
        try:
            if max(cols.values()) >= len(row):
                raise UserInputError("missing columns")
            lots.append(
                {
                    "symbol": _symbol_from(row, cols),
                    "qty": parse_quantity(row[cols["qty"]]),
                    "avg_price": parse_price(row[cols["avg_price"]]),
                    "buy_date": parse_buy_date(row[cols["buy_date"]]) if "buy_date" in cols else None,
                }
            )
        except UserInputError as e:
            label = next(
                (row[cols[f]] for f in ("symbol", "name", "isin") if f in cols and cols[f] < len(row)), ""
            )
            problems.append(f"Row {n} ({md_cell(label.strip()[:30]) or '?'}): {e.user_message}")
    return lots, problems


# ── holdings and P&L ─────────────────────────────────────────────────────────


def aggregate(lots: list[dict]) -> list[dict[str, Any]]:
    """Lots added up per symbol: total qty, invested, weighted average price."""
    by: dict[str, dict[str, Any]] = {}
    for lot in lots:
        h = by.setdefault(
            lot["symbol"], {"symbol": lot["symbol"], "qty": Decimal(0), "invested": Decimal(0), "lots": 0}
        )
        qty, price = _dec(lot["qty"]), _dec(lot["avg_price"])
        h["qty"] += qty
        h["invested"] += qty * price
        h["lots"] += 1
    for h in by.values():
        h["avg_price"] = (h["invested"] / h["qty"]).quantize(Decimal("0.0001")) if h["qty"] else Decimal(0)
    return sorted(by.values(), key=lambda h: h["symbol"])


def _live_price(symbol: str, quote: Callable[[str], dict]) -> Decimal | None:
    try:
        info = quote(symbol) or {}
    except Exception:
        return None
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    return (
        _dec(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None
    )


def valuation(
    lots: list[dict], quote: Callable[[str], dict] | None = None
) -> tuple[list[dict], dict[str, Any]]:
    """(holdings, summary) at live prices. A holding without a price is
    valued at cost and flagged `price_missing`."""
    if quote is None:
        from tools.data.providers import market_data

        def quote(s: str) -> dict:
            return market_data().quote(s)

    holdings = []
    total_inv = total_val = Decimal(0)
    missing = []
    for h in aggregate(lots):
        price = _live_price(h["symbol"], quote)
        if price is None:
            missing.append(h["symbol"])
        value = h["qty"] * (price if price is not None else h["avg_price"])
        pnl = value - h["invested"]
        total_inv += h["invested"]
        total_val += value
        holdings.append(
            {
                **h,
                "current_price": price,
                "current_value": value,
                "pnl": pnl,
                "pnl_pct": float(pnl / h["invested"] * 100) if h["invested"] else 0.0,
                "price_missing": price is None,
            }
        )
    for h in holdings:
        h["weight_pct"] = float(h["current_value"] / total_val * 100) if total_val else 0.0
    total_pnl = total_val - total_inv
    summary = {
        "total_invested": total_inv.quantize(_CENTS),
        "total_value": total_val.quantize(_CENTS),
        "total_pnl": total_pnl.quantize(_CENTS),
        "total_pnl_pct": float(total_pnl / total_inv * 100) if total_inv else 0.0,
        "n_holdings": len(holdings),
        "prices_missing": missing,
    }
    return holdings, summary


def get_live(user_id: int, quote: Callable[[str], dict] | None = None) -> tuple[list[dict], dict[str, Any]]:
    from utils.db import portfolio_all

    return valuation(portfolio_all(user_id), quote)


def _inr(v: Decimal | float, signed: bool = False) -> str:
    from utils.numbers import format_inr

    text = format_inr(abs(v))
    return (("+" if v >= 0 else "−") + text) if signed else text


def to_markdown(holdings: list[dict], summary: dict) -> str:
    if not holdings:
        return "Your portfolio is empty. Add holdings in the **📊 Portfolio** tab (or import a broker CSV)."
    lines = [
        f"**Invested** {_inr(summary['total_invested'])} · **Value** {_inr(summary['total_value'])} · "
        f"**P&L** {_inr(summary['total_pnl'], signed=True)} ({summary['total_pnl_pct']:+.2f}%)",
        "",
        "| Symbol | Qty | Avg price | Price | Value | P&L | P&L % | Weight |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for h in sorted(holdings, key=lambda x: x["current_value"], reverse=True):
        price = "—" if h["price_missing"] else _inr(h["current_price"])
        lines.append(
            f"| {md_cell(h['symbol'])} | {h['qty'].normalize():f} | {_inr(h['avg_price'])} | {price} | "
            f"{_inr(h['current_value'])} | {_inr(h['pnl'], signed=True)} | {h['pnl_pct']:+.2f}% | "
            f"{h['weight_pct']:.1f}% |"
        )
    if summary.get("prices_missing"):
        lines += ["", f"*No live price for {', '.join(summary['prices_missing'])} — valued at cost.*"]
    return "\n".join(lines)


def claims(holdings: list[dict], summary: dict) -> list[dict[str, Any]]:
    """Deterministic statements for the `portfolio_view` chat tool."""
    if not holdings:
        return [{"key": "portfolio.empty", "claim": "The user's portfolio has no holdings", "value": 0}]
    out = [
        {
            "key": "portfolio.total",
            "claim": f"Portfolio: {summary['n_holdings']} holdings, invested {_inr(summary['total_invested'])}, "
            f"now {_inr(summary['total_value'])}, P&L {_inr(summary['total_pnl'], signed=True)} "
            f"({summary['total_pnl_pct']:+.2f}%)",
            "value": float(summary["total_pnl"]),
        }
    ]
    for h in sorted(holdings, key=lambda x: x["current_value"], reverse=True)[:15]:
        out.append(
            {
                "key": f"portfolio.{h['symbol']}",
                "claim": f"{h['symbol']}: {h['qty'].normalize():f} shares at avg {_inr(h['avg_price'])}, "
                f"P&L {_inr(h['pnl'], signed=True)} ({h['pnl_pct']:+.2f}%), {h['weight_pct']:.1f}% of portfolio"
                + (" (no live price; valued at cost)" if h["price_missing"] else ""),
                "value": h["pnl_pct"],
            }
        )
    return out


def chart_rows(holdings: list[dict]) -> list[dict[str, float | str]]:
    """Float rows for charts/builder.portfolio_chart."""
    return [
        {
            "symbol": h["symbol"],
            "current_value": float(h["current_value"]),
            "pnl": float(h["pnl"]),
            "pnl_pct": h["pnl_pct"],
        }
        for h in holdings
    ]
