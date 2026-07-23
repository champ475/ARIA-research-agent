from unittest.mock import patch

from graph.tool_catalogue import catalogue_summary, get_tool, list_tools


class TestCatalogueRegistry:
    def test_expected_tools_registered(self):
        names = {spec.name for spec in list_tools()}
        assert names == {
            "fetch_info", "fetch_financials", "fetch_ohlcv", "fetch_news",
            "compute_fundamentals", "compute_technicals",
        }

    def test_get_tool_returns_matching_spec(self):
        spec = get_tool("fetch_info")
        assert spec.name == "fetch_info"
        assert spec.depends_on == ()

    def test_dependent_tools_declare_their_dependencies(self):
        assert get_tool("compute_fundamentals").depends_on == ("fetch_info", "fetch_financials")
        assert get_tool("compute_technicals").depends_on == ("fetch_ohlcv",)

    def test_catalogue_summary_lists_every_tool_with_deps(self):
        summary = catalogue_summary()
        assert "fetch_info" in summary
        assert "compute_fundamentals" in summary
        assert "needs: fetch_info, fetch_financials" in summary


class TestToolAdapters:
    def test_fetch_info_delegates_to_fetcher(self):
        with patch("graph.tool_catalogue.get_info", return_value={"pe": 24.3}) as mock_get_info:
            result = get_tool("fetch_info").fn("RELIANCE", {})
        mock_get_info.assert_called_once_with("RELIANCE")
        assert result == {"pe": 24.3}

    def test_compute_fundamentals_reads_from_context(self):
        context = {"fetch_info": {"a": 1}, "fetch_financials": {"b": 2}}
        with patch("graph.tool_catalogue.compute_fundamental_ratios", return_value={"pe": 24.3}) as mock_compute:
            result = get_tool("compute_fundamentals").fn("RELIANCE", context)
        mock_compute.assert_called_once_with({"a": 1}, {"b": 2})
        assert result == {"pe": 24.3}

    def test_compute_technicals_reads_ohlcv_from_context(self):
        fake_df = object()
        context = {"fetch_ohlcv": fake_df}
        with patch("graph.tool_catalogue.compute_technical_indicators", return_value="indicators") as mock_all, \
             patch("graph.tool_catalogue.technical_snapshot", return_value={"rsi": 55}) as mock_snap:
            result = get_tool("compute_technicals").fn("RELIANCE", context)
        mock_all.assert_called_once_with(fake_df)
        mock_snap.assert_called_once_with("indicators")
        assert result == {"rsi": 55}
