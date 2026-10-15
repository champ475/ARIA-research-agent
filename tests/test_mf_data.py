"""DATA-04: mutual fund categories, metrics and rankings. (The live accuracy
check against AMFI's published NAVs is scripts/verify_mf_returns.py: 36
returns across 12 schemes in 3 categories within 0.006 pp on 2026-10-14.)"""

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from tools.data.mf_data import (
    canonical_category,
    category_for_query,
    compute_metrics,
    parse_navall,
    rank_funds,
)

NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - ELSS)

Alpha Mutual Fund

1001;INF001;-;Alpha ELSS Tax Saver Fund;Direct Plan;GROWTH;65.55;13-Oct-2026
1002;INF002;-;Alpha ELSS Tax Saver Fund;Regular Plan;GROWTH;58.21;13-Oct-2026
1003;INF003;INF004;Alpha ELSS Tax Saver Fund;Direct Plan;IDCW;411.96;13-Oct-2026

Open Ended Schemes(Equity Schemes - ELSS- Tax Saver Fund)

Beta Mutual Fund

2001;INF201;-;Beta Tax Saver Fund;Direct Plan;Growth Option;30.10;13-Oct-2026
2002;INF202;-;Beta Tax Saver Fund;Direct Plan;Growth Option;N.A.;13-Oct-2026

Close Ended Schemes(Equity Scheme - ELSS)

Gamma Mutual Fund

3001;INF301;-;Gamma Long Term Advantage Series V;Direct Plan;Growth;32.88;13-Oct-2026

Open Ended Schemes(Equity Scheme - Large Cap Fund)

Delta Mutual Fund

4001;INF401;-;Delta Bluechip Fund - Direct Plan - Growth;99.5;13-Oct-2026
"""


def test_parses_new_8_field_and_old_6_field_rows():
    df = parse_navall(NAVALL)
    assert len(df) == 6  # the N.A. NAV row is skipped
    row = df[df.scheme_code == "1001"].iloc[0]
    assert (row.category, row.plan, row.option, row.nav, row.amc) == (
        "ELSS",
        "direct",
        "growth",
        65.55,
        "Alpha Mutual Fund",
    )
    assert df[df.scheme_code == "1003"].iloc[0].option == "idcw"
    old = df[df.scheme_code == "4001"].iloc[0]  # 6-field layout: plan/option read from the name
    assert (old.category, old.plan, old.option, old.nav) == ("Large Cap", "direct", "growth", 99.5)
    assert df[df.scheme_code == "3001"].iloc[0].scheme_type == "close"


@pytest.mark.parametrize(
    "header,name",
    [
        ("Equity Scheme - ELSS", "ELSS"),
        ("Equity Schemes - ELSS- Tax Saver Fund", "ELSS"),
        ("Equity Scheme - Large & Mid Cap Fund", "Large & Mid Cap"),
        ("Income/Debt Oriented Schemes - Short Term Fund", "Short Duration"),
        ("Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage", "Balanced Advantage"),
        ("Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation", "Balanced Advantage"),
        ("Other Scheme - Index Funds", "Index Fund"),
        ("Index Funds - Debt Funds", "Index Fund (Debt)"),
        ("Equity Schemes - Sectoral Fund", "Sectoral/Thematic"),
    ],
)
def test_category_variants_are_one_canonical_name(header, name):
    assert canonical_category(header) == name


@pytest.mark.parametrize(
    "query,category",
    [
        ("best ELSS funds", "ELSS"),
        ("tax saving funds", "ELSS"),
        ("top bluechip funds", "Large Cap"),
        ("large and mid cap", "Large & Mid Cap"),
        ("flexicap", "Flexi Cap"),
        ("debt funds", "Short Duration"),
        ("tell me about TCS", None),
    ],
)
def test_query_to_category(query, category):
    assert category_for_query(query) == category


def _series(start: date, years: float, annual_rate: float) -> pd.Series:
    days = int(years * 365.25)
    idx = [start + timedelta(days=i) for i in range(days + 1)]
    daily = (1 + annual_rate) ** (1 / 365.25)
    return pd.Series([100 * daily**i for i in range(days + 1)], index=idx)


def test_metrics_on_a_known_series():
    m = compute_metrics(_series(date(2020, 1, 1), 6, 0.12))
    for key in ("cagr_1y", "cagr_3y", "cagr_5y"):
        assert m[key] == pytest.approx(12.0, abs=0.1)
    assert m["volatility_1y"] == pytest.approx(0.0, abs=0.01)
    assert m["max_drawdown_3y"] == pytest.approx(0.0, abs=0.01)
    assert m["consistency_1y"] == 100.0


def test_short_history_gives_no_long_period_returns():
    m = compute_metrics(_series(date(2024, 6, 1), 2, 0.10))
    assert m["cagr_1y"] is not None and m["cagr_3y"] is None and m["cagr_5y"] is None


def test_drawdown_is_measured():
    s = _series(date(2020, 1, 1), 4, 0.10)
    s.iloc[-400:-300] *= 0.7  # a 30% dip within the last 3 years
    assert compute_metrics(s)["max_drawdown_3y"] == pytest.approx(-30.0, abs=1.0)


def test_rank_funds_only_open_direct_growth_of_the_category_ranked_with_method():
    nav = parse_navall(NAVALL)
    metrics = {
        "1001": {"cagr_5y": 12.0, "volatility_1y": 15.0},
        "2001": {"cagr_5y": 14.0, "volatility_1y": 18.0},
    }
    with patch("tools.data.mf_data.metrics_for", return_value=metrics):
        out = rank_funds("best ELSS funds", horizon_years=10, nav_df=nav)
    assert list(out.scheme_code) == ["2001", "1001"]  # 1002 regular, 1003 IDCW, 3001 closed-ended excluded
    assert list(out["rank"]) == [1, 2]
    assert out.ranking_method.iloc[0].startswith("ranked by 5-year CAGR")


def test_young_funds_follow_the_ranked_ones_and_short_horizons_use_short_returns():
    nav = parse_navall(NAVALL)
    metrics = {"1001": {"cagr_1y": 3.0, "cagr_5y": None}, "2001": {"cagr_1y": 9.0, "cagr_5y": 10.0}}
    with patch("tools.data.mf_data.metrics_for", return_value=metrics):
        five = rank_funds("elss", horizon_years=7, nav_df=nav)
        one = rank_funds("elss", horizon_years=1, nav_df=nav)
    assert list(five.scheme_code) == ["2001", "1001"] and pd.isna(five["rank"].iloc[1])
    assert list(one.scheme_code) == ["2001", "1001"] and "1-year return" in one.ranking_method.iloc[0]


def test_planning_ranks_by_the_investors_horizon():
    from utils import planning_logic

    with patch.object(planning_logic, "get_category_funds", return_value=pd.DataFrame()) as gcf:
        planning_logic.fetch_fund_data({"horizon_years": 12, "tax_regime": "old"})
    assert all(call.kwargs["horizon_years"] == 12.0 for call in gcf.call_args_list)
