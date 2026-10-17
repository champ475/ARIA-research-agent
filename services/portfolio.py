"""
services/portfolio.py — the Portfolio tab's operations (ARCH-02, UX-05).

The UI passes what the user typed; these functions parse it (tools/portfolio.py),
store it (utils/db.py) and audit it. They raise UserInputError with a message
fit to show, or PortfolioFull; nothing here knows about Gradio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tools import portfolio
from tools.charts.builder import portfolio_chart
from utils.audit import audit
from utils.db import MAX_PORTFOLIO_LOTS, PortfolioFull
from utils.errors import UserInputError

__all__ = ["MAX_PORTFOLIO_LOTS", "PortfolioFull", "UserInputError"]


def view(user_id: int) -> tuple[str, Any, list[dict]]:
    """(summary markdown, allocation chart or None, lots) for the Portfolio tab."""
    from utils.db import portfolio_all

    lots = portfolio_all(user_id)
    holdings, summary = portfolio.valuation(lots)
    chart = portfolio_chart(portfolio.chart_rows(holdings)) if holdings else None
    return portfolio.to_markdown(holdings, summary), chart, lots


def add_lot(user_id: int, symbol: Any, qty: Any, price: Any, buy_date: Any) -> tuple[str, Decimal]:
    """Parse and store one lot; returns (symbol, quantity)."""
    from utils.db import portfolio_add

    lot = portfolio.parse_lot(symbol, qty, price, buy_date)
    portfolio_add(user_id, lot["symbol"], lot["qty"], lot["avg_price"], lot["buy_date"])
    return lot["symbol"], lot["qty"]


def update_lot(user_id: int, lot_id: Any, qty: Any, price: Any, buy_date: Any) -> bool:
    from utils.db import portfolio_update

    return bool(lot_id) and portfolio_update(
        user_id,
        int(lot_id),
        portfolio.parse_quantity(qty),
        portfolio.parse_price(price),
        portfolio.parse_buy_date(buy_date),
    )


def remove_lot(user_id: int, lot_id: Any) -> bool:
    from utils.db import portfolio_remove_lot

    return bool(lot_id) and portfolio_remove_lot(user_id, int(lot_id))


def remove_symbol(user_id: int, symbol: Any) -> tuple[str, int]:
    """(cleaned symbol, lots removed)."""
    from utils.db import portfolio_remove

    sym = (symbol or "").strip().upper()
    return sym, (portfolio_remove(user_id, sym) if sym else 0)


def import_csv(user_id: int, path: str, replace: bool) -> tuple[int, list[str]]:
    """Import a broker holdings export from an uploaded file. All or nothing.
    Returns (lots imported, problems found in skipped rows)."""
    from utils.db import portfolio_import

    with open(path, "rb") as f:
        data = f.read(portfolio.MAX_CSV_BYTES + 1)
    lots, problems = portfolio.parse_csv(data)
    if not lots:
        raise UserInputError("No usable holdings in that file.")
    n = portfolio_import(user_id, lots, replace=bool(replace))
    audit("portfolio.import", actor_user_id=user_id, details={"lots": n, "replace": bool(replace)})
    return n, problems
