"""LLM-04: quote_info yields only allowlisted, labelled conclusions — and the
synthesis prompt shrinks by at least 30% on an equity fixture."""

from __future__ import annotations

from datetime import UTC, datetime

from graph.nodes.conclusion_builder import _conclusions_from_evidence
from graph.quote_fields import QUOTE_FIELDS, display_value, quote_rows

# A realistic slice of yfinance's ~150-field info dict.
INFO = {
    "currentPrice": 3512.4,
    "previousClose": 3490.0,
    "open": 3495.5,
    "dayHigh": 3530.0,
    "dayLow": 3480.25,
    "fiftyTwoWeekHigh": 4592.25,
    "fiftyTwoWeekLow": 3056.05,
    "marketCap": 12_730_000_000_000,
    "trailingPE": 25.8,
    "forwardPE": 23.1,
    "priceToBook": 12.9,
    "trailingEps": 136.1,
    "returnOnEquity": 0.512,
    "profitMargins": 0.191,
    "revenueGrowth": 0.06,
    "beta": 0.62,
    "volume": 2_314_000,
    "averageVolume": 2_890_000,
    "targetMeanPrice": 3900.0,
    "trailingAnnualDividendYield": 0.0172,
    # the trivia that used to become conclusions
    "maxAge": 86400,
    "gmtOffSetMilliseconds": 19800000,
    "fullTimeEmployees": 607354,
    "firstTradeDateEpochUtc": 1030000000,
    "priceHint": 2,
    "sharesOutstanding": 3_618_000_000,
    "floatShares": 1_000_000_000,
    "impliedSharesOutstanding": 3_620_000_000,
    "heldPercentInsiders": 0.72,
    "heldPercentInstitutions": 0.21,
    "enterpriseValue": 12_500_000_000_000,
    "enterpriseToRevenue": 5.1,
    "enterpriseToEbitda": 18.2,
    "52WeekChange": -0.12,
    "SandP52WeekChange": 0.18,
    "lastDividendValue": 30,
    "lastDividendDate": 1720000000,
    "exDividendDate": 1720000000,
    "totalCash": 500_000_000_000,
    "totalDebt": 80_000_000_000,
    "quickRatio": 2.1,
    "currentRatio": 2.4,
    "totalRevenue": 2_500_000_000_000,
    "grossProfits": 1_000_000_000_000,
    "ebitda": 700_000_000_000,
    "operatingCashflow": 450_000_000_000,
    "freeCashflow": 400_000_000_000,
    "auditRisk": 4,
    "boardRisk": 2,
    "compensationRisk": 3,
    "overallRisk": 2,
    "governanceEpochDate": 1700000000,
    "longName": "Tata Consultancy Services Limited",
    "sector": "Technology",
}


def _ev(payload):
    return {
        "id": "ev-1",
        "source": "fetch_info",
        "tool": "fetch_info",
        "entity": "TCS",
        "evidence_type": "quote_info",
        "payload": payload,
        "timestamp": datetime.now(UTC),
        "confidence": 0.9,
        "citation_key": "quote_info",
    }


def test_only_allowlisted_fields_become_conclusions():
    conclusions = _conclusions_from_evidence(_ev(INFO))
    keys = {c["citation_key"].split(".", 1)[1] for c in conclusions}
    assert keys <= set(QUOTE_FIELDS)
    assert not any(t in str(conclusions) for t in ("maxAge", "gmtOffSet", "fullTimeEmployees", "auditRisk"))
    assert len(conclusions) == len(quote_rows(INFO)) == 20


def test_values_are_in_human_units():
    claims = {c["citation_key"]: c["claim"] for c in _conclusions_from_evidence(_ev(INFO))}
    assert claims["TCS.marketCap"] == "TCS Market cap = ₹1,273,000.00 crore"
    assert claims["TCS.currentPrice"] == "TCS Price = ₹3,512.40"
    assert claims["TCS.returnOnEquity"] == "TCS ROE (reported) = 51.2%"
    assert claims["TCS.trailingPE"] == "TCS P/E (trailing) = 25.8"


def test_regular_market_price_fills_in_for_current_price():
    rows = quote_rows({"regularMarketPrice": 101.5})
    assert rows == [("currentPrice", "Price", 101.5, "₹")]


def test_prompt_size_drops_by_at_least_30_percent():
    numeric = {k: v for k, v in INFO.items() if isinstance(v, (int, float))}
    old_claims = "\n".join(f"TCS {k} = {v}" for k, v in numeric.items())  # the pre-LLM-04 rendering
    new_claims = "\n".join(c["claim"] for c in _conclusions_from_evidence(_ev(INFO)))
    assert len(new_claims) <= 0.7 * len(old_claims)


def test_display_value_units():
    assert display_value(1.5, "%") == "1.5%" and display_value(2314000, "shares") == "2,314,000 shares"
