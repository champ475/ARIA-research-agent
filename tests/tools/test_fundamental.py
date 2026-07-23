"""Tests for tools/analysis/fundamental.py — ratio recomputation and DCF."""
import pandas as pd
import pytest

from tools.analysis import fundamental


def _bs():
    return pd.DataFrame(
        {"2024": [1000.0, 400.0, 200.0, 600.0, 300.0, 50.0]},
        index=["Total Assets", "Total Current Assets", "Total Current Liabilities",
               "Total Stockholder Equity", "Total Debt", "Cash And Cash Equivalents"],
    )


def _inc():
    return pd.DataFrame(
        {"2024": [500.0, 60.0, 90.0, 100.0]},
        index=["Total Revenue", "Net Income", "Ebit", "Gross Profit"],
    )


def _cf():
    return pd.DataFrame(
        {"2024": [80.0, -20.0]},
        index=["Total Cash From Operating Activities", "Capital Expenditure"],
    )


def _info(**overrides):
    d = {
        "currentPrice": 150.0, "marketCap": 30000.0, "beta": 1.1,
        "trailingEps": 10.0, "bookValue": 20.0, "debtToEquity": 50.0,
        "longName": "Test Co", "sector": "IT", "industry": "Software",
    }
    d.update(overrides)
    return d


class TestExtractRaw:
    def test_extracts_expected_fields(self):
        raw = fundamental._extract_raw(_bs(), _inc(), _cf())
        assert raw["total_assets"] == 1000.0
        assert raw["current_assets"] == 400.0
        assert raw["current_liab"] == 200.0
        assert raw["total_equity"] == 600.0
        assert raw["total_debt"] == 300.0
        assert raw["revenue"] == 500.0
        assert raw["net_income"] == 60.0
        assert raw["ebit"] == 90.0

    def test_fcf_computed_from_ocf_and_capex(self):
        raw = fundamental._extract_raw(_bs(), _inc(), _cf())
        assert raw["fcf"] == pytest.approx(80.0 - abs(-20.0))

    def test_empty_frames_return_none_fields(self):
        raw = fundamental._extract_raw(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        assert raw["total_assets"] is None
        assert raw["fcf"] is None


class TestComputeDE:
    def test_recomputes_from_balance_sheet(self):
        raw = {"total_debt": 300.0, "total_equity": 600.0}
        out = fundamental._compute_de(raw, {})
        assert out["value"] == 0.5

    def test_falls_back_to_upstream_percentage(self):
        out = fundamental._compute_de({}, {"debtToEquity": 150.0})
        assert out["value"] == 1.5  # 150 / 100

    def test_falls_back_to_upstream_ratio_when_small(self):
        out = fundamental._compute_de({}, {"debtToEquity": 1.2})
        assert out["value"] == 1.2

    def test_no_data_returns_none(self):
        out = fundamental._compute_de({}, {})
        assert out["value"] is None


class TestRatio:
    def test_basic_division(self):
        assert fundamental._ratio(50, 200, "x") == 0.25

    def test_none_numerator_returns_none(self):
        assert fundamental._ratio(None, 200, "x") is None

    def test_zero_denominator_returns_none(self):
        assert fundamental._ratio(50, 0, "x") is None


class TestDCF:
    def test_none_when_fcf_missing(self):
        assert fundamental._dcf({"fcf": None, "mktcap": 1000, "price": 100}) is None

    def test_none_when_fcf_negative(self):
        assert fundamental._dcf({"fcf": -10, "mktcap": 1000, "price": 100}) is None

    def test_produces_three_scenarios_bear_lt_base_lt_bull(self):
        out = fundamental._dcf({"fcf": 60.0, "mktcap": 30000.0, "price": 150.0})
        assert out is not None
        assert out["Bear"] < out["Base"] < out["Bull"]

    def test_shares_derived_from_mktcap_over_price(self):
        out = fundamental._dcf({"fcf": 60.0, "mktcap": 30000.0, "price": 150.0})
        assert out["shares"] == pytest.approx(200.0)


class TestComputeEndToEnd:
    def test_full_compute_returns_expected_ratios(self):
        financials = {"balance_annual": _bs(), "income_annual": _inc(), "cashflow_annual": _cf()}
        d = fundamental.compute(_info(), financials)
        assert d["price"] == 150.0
        assert d["de"] == 0.5
        assert d["roce"] == pytest.approx(round(90.0 / (1000.0 - 200.0) * 100, 2))
        assert d["current_ratio"] == pytest.approx(2.0)
        assert d["pe"] == pytest.approx(15.0)
        assert d["pb"] == pytest.approx(7.5)
        assert d["dcf"] is not None

    def test_missing_financials_does_not_crash(self):
        d = fundamental.compute(_info(), {})
        assert d["price"] == 150.0
        assert d["de"] is not None  # falls back to upstream debtToEquity
