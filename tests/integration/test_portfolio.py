"""UX-05 on a real Postgres: three holdings give P&L matching a hand
calculation, amounts are stored exactly, imports are all-or-nothing, the
chat tool sees only the asker's holdings, and a P&L alert fires."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import psycopg
import pytest

from scripts import check_alerts as ca
from tools import portfolio as pf

pytestmark = pytest.mark.integration

D = Decimal
PRICES = {"TCS": 3500.0, "INFY": 1400.5, "ITC": 450.0}


def _quote(sym):
    return {"currentPrice": PRICES[sym]}


def test_three_holdings_match_a_hand_calculation(db, two_users):
    a, _ = two_users
    db.portfolio_add(a, "TCS", D(10), D("3000.10"), date(2024, 1, 2))
    db.portfolio_add(a, "TCS", D(5), D("3600.40"))
    db.portfolio_add(a, "INFY", D("20.5"), D("1450"))
    db.portfolio_add(a, "ITC", D(100), D("410.25"))

    holdings, s = pf.get_live(a, quote=_quote)
    by = {h["symbol"]: h for h in holdings}
    # Hand calculation:
    #   TCS  15 x 3500    = 52,500.00 - (30,001.00 + 18,002.00) = +4,497.00
    #   INFY 20.5 x 1400.5 = 28,710.25 - 29,725.00              = -1,014.75
    #   ITC  100 x 450     = 45,000.00 - 41,025.00              = +3,975.00
    assert (by["TCS"]["pnl"], by["INFY"]["pnl"], by["ITC"]["pnl"]) == (
        D("4497.0"),
        D("-1014.75"),
        D("3975.00"),
    )
    assert s["total_invested"] == D("118753.00") and s["total_value"] == D("126210.25")
    assert s["total_pnl"] == D("7457.25")
    assert round(s["total_pnl_pct"], 4) == round(7457.25 / 118753 * 100, 4)


def test_amounts_are_stored_exactly_and_lots_edit(db, two_users):
    a, b = two_users
    lot = db.portfolio_add(a, "TCS", D("0.0001"), D("1234567.8912"))
    row = db.portfolio_all(a)[0]
    assert row["qty"] == D("0.0001") and row["avg_price"] == D("1234567.8912")
    assert not db.portfolio_update(b, lot, D(1), D(1))  # not B's lot
    assert db.portfolio_update(a, lot, D(3), D("100.5"), date(2025, 5, 1))
    assert db.portfolio_all(a)[0]["buy_date"] == date(2025, 5, 1)
    assert not db.portfolio_remove_lot(b, lot)
    assert db.portfolio_remove_lot(a, lot) and db.portfolio_all(a) == []
    with pytest.raises(psycopg.errors.CheckViolation):
        db.portfolio_add(a, "TCS", D(0), D(1))


def test_import_is_all_or_nothing_and_capped(db, two_users, monkeypatch):
    a, _ = two_users
    db.portfolio_add(a, "TCS", D(1), D(1))
    lots = [
        {"symbol": "ITC", "qty": D(1), "avg_price": D(400)},
        {"symbol": "INFY", "qty": D(2), "avg_price": D(1)},
    ]
    assert db.portfolio_import(a, lots) == 2 and len(db.portfolio_all(a)) == 3
    assert db.portfolio_import(a, lots[:1], replace=True) == 1
    assert [r["symbol"] for r in db.portfolio_all(a)] == ["ITC"]

    monkeypatch.setattr(db, "MAX_PORTFOLIO_LOTS", 2)
    with pytest.raises(db.PortfolioFull):
        db.portfolio_import(a, lots)
    assert [r["symbol"] for r in db.portfolio_all(a)] == ["ITC"]  # nothing half-imported
    db.portfolio_add(a, "TCS", D(1), D(1))
    with pytest.raises(db.PortfolioFull):
        db.portfolio_add(a, "SBIN", D(1), D(1))
    assert db.portfolio_remove(a, "TCS") == 1


def test_chat_tool_sees_only_the_askers_holdings(db, two_users):
    from graph.tool_catalogue import get_tool

    a, b = two_users
    db.portfolio_add(a, "TCS", D(10), D(3000))
    db.portfolio_add(b, "ITC", D(1), D(1))
    spec = get_tool("portfolio_view")
    assert spec.cacheable is False and not spec.side_effect
    with patch("tools.data.providers.market_data") as md:
        md.return_value.quote.side_effect = _quote
        out = spec.fn("market", {"_user_id": a})
    text = " ".join(c["claim"] for c in out["claims"])
    assert "TCS" in text and "ITC" not in text and "+₹5,000.00" in text


def test_portfolio_pnl_alert_fires(db, two_users):
    a, _ = two_users
    db.portfolio_add(a, "TCS", D(10), D(3000))  # +16.67% at 3,500
    db.portfolio_add(a, "TCS", D(10), D(3000))
    alert_id = db.alert_add(a, "TCS", "portfolio_pnl", "TCS holding P&L up 10%", 10.0, direction="up")
    alert = db._query("SELECT * FROM alerts WHERE id=%s", (alert_id,), fetch="one")
    conn = db.get_conn()
    try:
        with patch.object(ca, "get_info", return_value={"currentPrice": 3500.0}):
            msg = ca.evaluate(conn, alert, ca.RunCache())
            assert msg and "+16.67%" in msg
        with patch.object(ca, "get_info", return_value={"currentPrice": 3100.0}):  # +3.33%
            assert ca.evaluate(conn, alert, ca.RunCache()) is None
    finally:
        db.release_conn(conn)
