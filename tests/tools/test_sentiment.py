"""Tests for tools/analysis/sentiment.py."""

from tools.analysis import sentiment


class TestScoreText:
    def test_bullish_text_scores_positive(self):
        s = sentiment.score_text("Company profits surge, record high growth beats expectations")
        assert s > 0

    def test_bearish_text_scores_negative(self):
        s = sentiment.score_text("Company reports huge loss, fraud probe by SEBI, stock crashes")
        assert s < 0

    def test_neutral_text_near_zero(self):
        s = sentiment.score_text("The quarterly board meeting is scheduled for next week.")
        assert -0.35 < s < 0.35

    def test_score_bounded(self):
        s = sentiment.score_text("surge surge surge record profit growth beat outperform buy")
        assert -1.0 <= s <= 1.0


class TestLabel:
    def test_bullish_label(self):
        assert sentiment.label(0.5) == "🟢 Bullish"

    def test_slightly_bullish_label(self):
        assert sentiment.label(0.2) == "🟡 Slightly Bullish"

    def test_neutral_label(self):
        assert sentiment.label(0.0) == "⚪ Neutral"

    def test_slightly_bearish_label(self):
        assert sentiment.label(-0.2) == "🟠 Slightly Bearish"

    def test_bearish_label(self):
        assert sentiment.label(-0.5) == "🔴 Bearish"


class TestAnalyse:
    def test_empty_news_returns_neutral(self):
        out = sentiment.analyse([])
        assert out["score"] == 0.0
        assert out["label"] == "⚪ Neutral"
        assert out["items"] == []

    def test_mixed_news_produces_average_score(self):
        items = [
            {"title": "Company surges to record profit", "summary": "strong growth beat"},
            {"title": "Company faces fraud probe", "summary": "loss reported, stock crashes"},
        ]
        out = sentiment.analyse(items)
        assert len(out["items"]) == 2
        assert isinstance(out["score"], float)

    def test_output_has_expected_keys(self):
        items = [{"title": "Company profits surge", "summary": "record growth"}]
        out = sentiment.analyse(items)
        for key in ("score", "label", "items", "bull_kws", "bear_kws", "text"):
            assert key in out
