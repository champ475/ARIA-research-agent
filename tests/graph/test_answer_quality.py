"""WP8 (smoke test 2026-10-17): units, one ROE, statement currency, lender rows, footnote markers."""

from datetime import UTC, datetime

import pytest

from graph.guardrails import finalize
from graph.nodes.conclusion_builder import conclusion_builder
from tools.analysis.fundamental import compute, number_footnotes


def _ev(etype, payload, entity="HDFCBANK", eid=None):
    return {
        "id": eid or f"{etype}-{entity}",
        "source": "t",
        "tool": "t",
        "entity": entity,
        "evidence_type": etype,
        "payload": payload,
        "timestamp": datetime.now(UTC),
        "confidence": 1.0,
        "citation_key": etype,
    }


def _claims(evidence):
    return {c["citation_key"]: c["claim"] for c in conclusion_builder({"evidence": evidence})["conclusions"]}


class TestConclusionUnitsAndTwins:
    def test_amounts_are_in_crore_and_ratios_are_percentages_with_labels(self):
        claims = _claims(
            [
                _ev(
                    "fundamental_ratio",
                    {"revenue": 1.9e12, "roa": 0.0134, "nim_proxy": 0.0285, "ps": 3.8, "roce": 15.2},
                )
            ]
        )
        assert claims["HDFCBANK.revenue"] == "HDFCBANK Revenue (latest year) = ₹190,000.00 crore"
        assert claims["HDFCBANK.roa"] == "HDFCBANK ROA = 1.34%"
        assert (
            "Net interest margin (proxy" in claims["HDFCBANK.nim_proxy"]
            and "2.85%" in claims["HDFCBANK.nim_proxy"]
        )
        assert claims["HDFCBANK.ps"] == "HDFCBANK Price/Sales = 3.80"
        assert claims["roce"] == "HDFCBANK ROCE = 15.20%"
        assert not any("nim_proxy =" in c for c in claims.values())

    def test_one_roe_the_reported_one_when_the_quote_has_it(self):
        quote = _ev("quote_info", {"returnOnEquity": 0.1384, "currentPrice": 728.85})
        computed = _ev("fundamental_ratio", {"roe": 0.0863, "roa": 0.0134})
        claims = _claims([quote, computed])
        roe = [c for c in claims.values() if "ROE" in c]
        assert len(roe) == 1 and "13.84%" in roe[0]

    def test_computed_roe_is_used_when_the_quote_lacks_it(self):
        claims = _claims(
            [_ev("quote_info", {"currentPrice": 1.0}), _ev("fundamental_ratio", {"roe": 0.0863})]
        )
        assert any("ROE = 8.63%" in c for c in claims.values())

    def test_duplicates_of_the_quote_are_dropped_but_pe_pb_keep_their_footnote_keys(self):
        quote = _ev(
            "quote_info",
            {"trailingPE": 15.9, "priceToBook": 1.85, "trailingEps": 45.7, "profitMargins": 0.268},
        )
        computed = _ev(
            "fundamental_ratio",
            {"pe": 15.93, "pb": 1.85, "eps": 45.75, "net_margin": 0.2679, "employees": 212958},
        )
        claims = _claims([quote, computed])
        assert "pe" in claims and "pb" in claims  # [^pe] / [^pb] need them
        assert "HDFCBANK.eps" not in claims and "HDFCBANK.net_margin" not in claims
        assert not any("employees" in k for k in claims)


class TestStatementCurrency:
    INFO = {
        "currency": "INR",
        "financialCurrency": "USD",
        "marketCap": 4.7e12,
        "currentPrice": 1161.2,
        "priceToSalesTrailing12Months": 231.7,
        "enterpriseToEbitda": 1048.7,
        "enterpriseValue": 4.6e12,
        "totalRevenue": 2.03e10,
        "ebitda": 5.0e9,
        "trailingEps": 73.4,
        "bookValue": 224.0,
    }

    def test_usd_statements_are_converted_before_meeting_an_inr_market_cap(self):
        d = compute(self.INFO, {}, fx=88.0)
        assert d["revenue"] == pytest.approx(2.03e10 * 88)
        assert d["ps"] == pytest.approx(4.7e12 / (2.03e10 * 88), abs=0.01)
        assert d["ps"] < 5 and d["ev_ebitda"] < 20
        assert "USD" in d["currency_note"] and "88.00" in d["currency_note"]

    def test_without_a_rate_amounts_and_the_mixed_multiples_are_not_shown(self):
        d = compute(self.INFO, {}, fx=None)
        assert d["revenue"] is None and d["ps"] is None and d["ev_ebitda"] is None
        assert "could not be converted" in d["currency_note"]

    def test_same_currency_is_left_alone(self):
        d = compute(
            {
                **self.INFO,
                "financialCurrency": "INR",
                "priceToSalesTrailing12Months": 3.1,
                "enterpriseToEbitda": 14.0,
            },
            {},
        )
        assert d["ps"] == 3.1 and d["ev_ebitda"] == 14.0 and "currency_note" not in d

    def test_an_absurd_multiple_is_not_shown(self):
        d = compute(
            {"currency": "INR", "priceToSalesTrailing12Months": 231.7, "enterpriseToEbitda": 1048.7}, {}
        )
        assert d["ps"] is None and d["ev_ebitda"] is None


def test_lender_rows_that_mean_nothing_for_a_bank_are_not_shown():
    info = {
        "sector": "Financial Services",
        "industry": "Banks - Regional",
        "currency": "INR",
        "enterpriseToEbitda": 9.0,
    }
    d = compute(info, {})
    for key in ("total_debt", "fcf", "ebitda", "op_margin", "ev_ebitda", "roce", "de"):
        assert d[key] is None, key


class TestFootnoteNumbers:
    BLOCK = "\n\n---\n### 🧮 Cited calculations\n\n**[^pe] P/E Ratio**  \nP/E = 1 / 2\n\n**[^pb] P/B Ratio**  \nP/B = 3 / 4\n"

    def test_markers_become_numbers_in_order_in_text_and_list(self):
        out = number_footnotes("P/E of 15.9 [^pe] and P/B of 1.85 [^pb]." + self.BLOCK)
        assert "15.9¹ and P/B of 1.85²." in out
        assert "**¹ P/E Ratio**" in out and "**² P/B Ratio**" in out
        assert "[^" not in out

    def test_a_repeated_marker_keeps_its_number(self):
        out = number_footnotes("A [^pb] then B [^pe] then C [^pb]." + self.BLOCK)
        assert "A¹ then B² then C¹." in out

    def test_markers_with_no_list_are_dropped(self):
        assert number_footnotes("P/E of 15.9 [^pe] today.") == "P/E of 15.9 today."

    def test_text_without_markers_is_untouched(self):
        assert number_footnotes("nothing to see") == "nothing to see"

    def test_finalize_numbers_them(self):
        out = finalize("P/E 15.9 [^pe]." + self.BLOCK, {"intent": "equity", "tasks": [1]})
        assert "15.9¹." in out and "[^pe]" not in out
