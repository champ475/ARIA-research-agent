"""Tests for tools/analysis/risk.py — multi-factor risk scoring."""

from tools.analysis import risk


def _base_fund(**overrides):
    d = {"pe": 20, "de": 0.5, "current_ratio": 1.8, "roce": 18, "roe": 15, "net_margin": 0.12}
    d.update(overrides)
    return d


def _base_ta(**overrides):
    d = {"ATR": 2.0, "current_price": 100.0}
    d.update(overrides)
    return d


class TestAssess:
    def test_healthy_company_scores_low(self):
        out = risk.assess(_base_fund(), _base_ta(), sentiment_score=0.2, macro={})
        assert out["score"] < 30
        assert out["level"] == "🟢 Low"

    def test_high_pe_flagged(self):
        out = risk.assess(_base_fund(pe=80), _base_ta(), sentiment_score=0.0, macro={})
        assert out["factors"]["Valuation"] == 80
        assert any("Very high P/E" in f for f in out["flags"])

    def test_negative_pe_flagged(self):
        out = risk.assess(_base_fund(pe=-5), _base_ta(), sentiment_score=0.0, macro={})
        assert out["factors"]["Valuation"] == 75
        assert any("Negative P/E" in f for f in out["flags"])

    def test_high_leverage_flagged(self):
        out = risk.assess(_base_fund(de=5.0), _base_ta(), sentiment_score=0.0, macro={})
        assert out["factors"]["Leverage"] == 90
        assert any("Very high D/E" in f for f in out["flags"])

    def test_low_current_ratio_flagged(self):
        out = risk.assess(_base_fund(current_ratio=0.5), _base_ta(), sentiment_score=0.0, macro={})
        assert out["factors"]["Liquidity"] == 80

    def test_negative_net_margin_dominates_profitability(self):
        out = risk.assess(
            _base_fund(roce=20, roe=20, net_margin=-0.1), _base_ta(), sentiment_score=0.0, macro={}
        )
        assert out["factors"]["Profitability"] == 85

    def test_high_atr_flagged_as_volatile(self):
        out = risk.assess(_base_fund(), _base_ta(ATR=8.0, current_price=100.0), sentiment_score=0.0, macro={})
        assert out["factors"]["Volatility"] == 80

    def test_bearish_sentiment_flagged(self):
        out = risk.assess(_base_fund(), _base_ta(), sentiment_score=-0.5, macro={})
        assert any("Bearish news sentiment" in f for f in out["flags"])

    def test_macro_inr_weakening_flagged(self):
        macro = {"USD/INR": {"change": 1.0}}
        out = risk.assess(_base_fund(), _base_ta(), sentiment_score=0.0, macro=macro)
        assert out["factors"]["Macro"] == 55

    def test_overall_score_is_weighted_sum(self):
        out = risk.assess(_base_fund(), _base_ta(), sentiment_score=0.0, macro={})
        weights = {
            "Valuation": 0.15,
            "Leverage": 0.20,
            "Liquidity": 0.15,
            "Profitability": 0.15,
            "Volatility": 0.15,
            "Sentiment": 0.10,
            "Macro": 0.10,
        }
        expected = round(sum(out["factors"].get(k, 0) * w for k, w in weights.items()), 1)
        assert out["score"] == expected

    def test_missing_fields_do_not_crash(self):
        out = risk.assess({}, {}, sentiment_score=0.0, macro=None)
        assert "score" in out
        assert out["level"] in {"🟢 Low", "🟡 Medium", "🟠 High", "🔴 Very High"}

    def test_score_bounds(self):
        out = risk.assess(
            _base_fund(pe=200, de=10, current_ratio=0.2, roce=1, roe=1, net_margin=-0.5),
            _base_ta(ATR=20, current_price=100),
            sentiment_score=-0.9,
            macro={"USD/INR": {"change": 2.0}, "Crude Oil": {"change": 5.0}},
        )
        assert 0 <= out["score"] <= 100
        assert out["level"] == "🔴 Very High"
