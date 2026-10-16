"""UX-05: holding input, lot aggregation, exact P&L and broker CSV import."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tools import portfolio as pf
from utils.errors import UserInputError

D = Decimal


@pytest.mark.parametrize(
    "raw, want", [("10", D(10)), ("1,000", D(1000)), (" 2.5 ", D("2.5")), (7, D(7)), ("0.0001", D("0.0001"))]
)
def test_quantity(raw, want):
    assert pf.parse_quantity(raw) == want


@pytest.mark.parametrize("raw", ["", "0", "-5", "ten", "1.23456", "5%", "₹10", "10 and 20"])
def test_bad_quantity(raw):
    with pytest.raises(UserInputError):
        pf.parse_quantity(raw)


@pytest.mark.parametrize(
    "raw, want",
    [("3450.5", D("3450.5")), ("₹3,450.50", D("3450.5")), ("Rs 1,23,456", D(123456)), ("0", D(0))],
)
def test_price(raw, want):
    assert pf.parse_price(raw) == want


@pytest.mark.parametrize("raw", ["", "abc", "12%", "-3", "99999999"])
def test_bad_price(raw):
    with pytest.raises(UserInputError):
        pf.parse_price(raw)


def test_buy_date():
    assert pf.parse_buy_date("") is None
    assert pf.parse_buy_date("2024-03-15") == date(2024, 3, 15)
    assert pf.parse_buy_date("15-03-2024") == date(2024, 3, 15)
    assert pf.parse_buy_date("15/03/2024") == date(2024, 3, 15)
    for bad in ["2024-13-01", "yesterday", (date.today() + timedelta(days=2)).isoformat(), "1985-01-01"]:
        with pytest.raises(UserInputError):
            pf.parse_buy_date(bad)


def test_parse_lot_resolves_company_names():
    lot = pf.parse_lot("Infosys", "10", "₹1,500", "")
    assert lot == {"symbol": "INFY", "qty": D(10), "avg_price": D("1500.0000"), "buy_date": None}
    with pytest.raises(UserInputError, match="Couldn't find"):
        pf.parse_lot("Zzqx Imaginary Corp", "1", "1")


def _lots():
    return [
        {"symbol": "TCS", "qty": D(10), "avg_price": D("3000.10")},
        {"symbol": "TCS", "qty": D(5), "avg_price": D("3600.40")},
        {"symbol": "INFY", "qty": D("20.5"), "avg_price": D("1450")},
        {"symbol": "ITC", "qty": D(100), "avg_price": D("410.25")},
    ]


def test_aggregate_weights_the_average_price():
    by = {h["symbol"]: h for h in pf.aggregate(_lots())}
    assert by["TCS"]["qty"] == 15 and by["TCS"]["lots"] == 2
    assert by["TCS"]["invested"] == D("30001.0") + D("18002.0")
    assert by["TCS"]["avg_price"] == D("3200.2000")


def test_valuation_matches_a_hand_calculation():
    prices = {"TCS": 3500.0, "INFY": 1400.5, "ITC": 0}  # ITC: no usable price
    holdings, s = pf.valuation(_lots(), quote=lambda sym: {"currentPrice": prices[sym]})
    by = {h["symbol"]: h for h in holdings}
    # TCS: 15 x 3500 = 52,500 vs 48,003 invested -> +4,497
    assert by["TCS"]["current_value"] == D("52500.0") and by["TCS"]["pnl"] == D("4497.0")
    # INFY: 20.5 x 1400.5 = 28,710.25 vs 29,725 -> -1,014.75
    assert by["INFY"]["pnl"] == D("-1014.75")
    # ITC has no price: valued at cost, flagged.
    assert by["ITC"]["price_missing"] and by["ITC"]["pnl"] == 0
    assert s["total_invested"] == D("48003.00") + D("29725.00") + D("41025.00")
    assert s["total_pnl"] == D("3482.25") and s["prices_missing"] == ["ITC"]
    assert round(sum(h["weight_pct"] for h in holdings), 6) == 100.0
    md = pf.to_markdown(holdings, s)
    assert "₹1,18,753.00" in md and "+₹3,482.25" in md and "No live price for ITC" in md
    assert pf.claims(holdings, s)[0]["claim"].startswith("Portfolio: 3 holdings")


def test_empty_portfolio():
    holdings, s = pf.valuation([], quote=lambda s: {})
    assert "portfolio is empty" in pf.to_markdown(holdings, s).lower()


ZERODHA = """Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.
TCS,10,3000.5,3500,35000,4995,16.6,0.5
INFY-BE,5,1500,1400,7000,-500,-6.6,-0.2
"""

GROWW = """Holdings Statement as on 15-10-2026
Name,Arnav
,
Stock Name,ISIN,Quantity,Average buy price,Buy value,Closing price,Closing value,Unrealised P&L
RELIANCE INDUSTRIES LTD,INE002A01018,4,"1,250.00",5000,1400,5600,600
Total,,,,5000,,5600,600
"""

GENERIC = """symbol;quantity;avg price;buy date
ITC;100;410.25;2024-01-15
NOSUCHCO;5;10;
HDFCBANK;-3;1600;
"""


def test_zerodha_holdings_export():
    lots, problems = pf.parse_csv(ZERODHA.encode())
    assert [(x["symbol"], x["qty"], x["avg_price"]) for x in lots] == [
        ("TCS", D(10), D("3000.5000")),
        ("INFY", D(5), D("1500.0000")),
    ]
    assert problems == []


def test_groww_export_with_a_title_block_and_isin():
    lots, problems = pf.parse_csv(GROWW)
    assert [(x["symbol"], x["qty"], x["avg_price"]) for x in lots] == [("RELIANCE", D(4), D("1250.0000"))]
    assert problems == []


def test_generic_sheet_reports_bad_rows_and_keeps_good_ones():
    lots, problems = pf.parse_csv(GENERIC)
    assert [x["symbol"] for x in lots] == ["ITC"] and lots[0]["buy_date"] == date(2024, 1, 15)
    assert len(problems) == 2 and problems[0].startswith("Row 3 (NOSUCHCO)") and "Row 4" in problems[1]


def test_unrecognised_or_oversized_files_are_refused():
    with pytest.raises(UserInputError, match="columns"):
        pf.parse_csv("a,b,c\n1,2,3\n")
    with pytest.raises(UserInputError, match="too large"):
        pf.parse_csv(b"x" * (pf.MAX_CSV_BYTES + 1))
