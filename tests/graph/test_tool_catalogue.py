from unittest.mock import patch

from graph.tool_catalogue import catalogue_summary, get_tool, list_tools


class TestCatalogueRegistry:
    def test_expected_tools_registered(self):
        names = {spec.name for spec in list_tools()}
        assert names == {
            "fetch_info",
            "fetch_financials",
            "fetch_ohlcv",
            "fetch_news",
            "compute_fundamentals",
            "compute_technicals",
            "fetch_quarterly_results",
            "fetch_annual_report_text",
            "fetch_nav",
            "fetch_category_funds",
            "fetch_mf_comparison",
            "fetch_mf_news",
            "alert_create",
            "alert_list",
            "alert_cancel",
            "alert_pause",
            "alert_resume",
            "screener_parse_query",
            "screener_run",
            "screener_to_markdown",
            "screener_save",
            "fetch_top_movers",
            "fetch_bulk_deals",
            "fetch_block_deals",
            # UX-04
            "peer_comparison",
            "risk_assessment",
            "news_sentiment",
            "backtest",
            "portfolio_view",  # UX-05
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
        with patch(
            "graph.tool_catalogue.compute_fundamental_ratios", return_value={"pe": 24.3}
        ) as mock_compute:
            result = get_tool("compute_fundamentals").fn("RELIANCE", context)
        mock_compute.assert_called_once_with({"a": 1}, {"b": 2})
        assert result == {"pe": 24.3}

    def test_compute_technicals_reads_ohlcv_from_context(self):
        fake_df = object()
        context = {"fetch_ohlcv": fake_df}
        with (
            patch("graph.tool_catalogue.compute_technical_indicators", return_value="indicators") as mock_all,
            patch("graph.tool_catalogue.technical_snapshot", return_value={"rsi": 55}) as mock_snap,
        ):
            result = get_tool("compute_technicals").fn("RELIANCE", context)
        mock_all.assert_called_once_with(fake_df)
        mock_snap.assert_called_once_with("indicators")
        assert result == {"rsi": 55}

    def test_fetch_quarterly_results_delegates_to_pdf_scraper(self):
        with patch("graph.tool_catalogue.get_latest_quarterly_results", return_value="Q3 text") as mock_fn:
            result = get_tool("fetch_quarterly_results").fn("RELIANCE", {})
        mock_fn.assert_called_once_with("RELIANCE")
        assert result == "Q3 text"

    def test_fetch_annual_report_text_delegates_to_pdf_scraper(self):
        with patch("graph.tool_catalogue.get_annual_report_text", return_value="AR text") as mock_fn:
            result = get_tool("fetch_annual_report_text").fn("RELIANCE", {})
        mock_fn.assert_called_once_with("RELIANCE")
        assert result == "AR text"

    def test_report_tools_have_no_dependencies(self):
        assert get_tool("fetch_quarterly_results").depends_on == ()
        assert get_tool("fetch_annual_report_text").depends_on == ()

    def test_fetch_nav_searches_full_nav_table(self):
        with (
            patch("graph.tool_catalogue.get_all_nav", return_value="nav_df") as mock_all_nav,
            patch("graph.tool_catalogue.search_fund", return_value="results") as mock_search,
        ):
            result = get_tool("fetch_nav").fn("HDFC Flexicap", {})
        mock_all_nav.assert_called_once()
        mock_search.assert_called_once_with("HDFC Flexicap", "nav_df")
        assert result == "results"

    def test_fetch_category_funds_delegates_with_top_n(self):
        with patch("graph.tool_catalogue.get_category_funds", return_value="funds") as mock_fn:
            result = get_tool("fetch_category_funds").fn("large cap", {})
        mock_fn.assert_called_once_with("large cap", top_n=10)
        assert result == "funds"

    def test_fetch_mf_comparison_splits_vs_separated_names(self):
        with patch("graph.tool_catalogue.get_mf_comparison", return_value="comparison") as mock_fn:
            get_tool("fetch_mf_comparison").fn("HDFC Flexicap vs ICICI Bluechip", {})
        mock_fn.assert_called_once_with(["HDFC Flexicap", "ICICI Bluechip"])

    def test_fetch_mf_comparison_single_name_falls_back_to_itself(self):
        with patch("graph.tool_catalogue.get_mf_comparison", return_value="comparison") as mock_fn:
            get_tool("fetch_mf_comparison").fn("HDFC Flexicap", {})
        mock_fn.assert_called_once_with(["HDFC Flexicap"])

    def test_fetch_mf_news_scopes_query_to_entity(self):
        with patch("graph.tool_catalogue.google_news", return_value=[{"title": "x"}]) as mock_fn:
            result = get_tool("fetch_mf_news").fn("ELSS", {})
        mock_fn.assert_called_once_with("India ELSS mutual fund", n=5)
        assert result == [{"title": "x"}]


class TestAlertToolAdapters:
    def test_alert_list_reads_user_id_from_context(self):
        with patch("graph.tool_catalogue.db_alert_list", return_value=[{"id": 1}]) as mock_fn:
            result = get_tool("alert_list").fn("TCS", {"_user_id": 42})
        mock_fn.assert_called_once_with(42, active_only=False)
        assert result == [{"id": 1}]

    def test_alert_cancel_removes_unique_match(self):
        alerts = [{"id": 5, "symbol": "TCS", "active": True}]
        with patch("graph.tool_catalogue.db_alert_remove") as mock_remove:
            result = get_tool("alert_cancel").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        mock_remove.assert_called_once_with(1, 5)
        assert result == {
            "action": "cancel",
            "symbol": "TCS",
            "result": "removed",
            "alert_id": 5,
            "condition": None,
        }

    def test_alert_cancel_not_found_when_no_match(self):
        with patch("graph.tool_catalogue.db_alert_remove") as mock_remove:
            result = get_tool("alert_cancel").fn("TCS", {"_user_id": 1, "alert_list": []})
        mock_remove.assert_not_called()
        assert result["result"] == "not_found"

    def test_alert_cancel_ambiguous_when_multiple_match(self):
        alerts = [{"id": 1, "symbol": "TCS"}, {"id": 2, "symbol": "TCS"}]
        result = get_tool("alert_cancel").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        assert result["result"] == "ambiguous"

    def test_alert_pause_only_matches_active_alerts(self):
        alerts = [{"id": 1, "symbol": "TCS", "active": False}]
        result = get_tool("alert_pause").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        assert result["result"] == "not_found"

    def test_alert_resume_only_matches_inactive_alerts(self):
        alerts = [{"id": 1, "symbol": "TCS", "active": True}]
        result = get_tool("alert_resume").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        assert result["result"] == "not_found"

    def test_alert_pause_succeeds_on_unique_active_match(self):
        alerts = [{"id": 4, "symbol": "TCS", "active": True}]
        with patch("graph.tool_catalogue.db_alert_pause") as mock_pause:
            result = get_tool("alert_pause").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        mock_pause.assert_called_once_with(1, 4)
        assert result == {
            "action": "pause",
            "symbol": "TCS",
            "result": "paused",
            "alert_id": 4,
            "condition": None,
        }

    def test_alert_resume_succeeds_on_unique_inactive_match(self):
        alerts = [{"id": 3, "symbol": "TCS", "active": False}]
        with patch("graph.tool_catalogue.db_alert_resume") as mock_resume:
            result = get_tool("alert_resume").fn("TCS", {"_user_id": 1, "alert_list": alerts})
        mock_resume.assert_called_once_with(1, 3)
        assert result == {
            "action": "resume",
            "symbol": "TCS",
            "result": "resumed",
            "alert_id": 3,
            "condition": None,
        }

    def test_alert_cancel_and_pause_and_resume_depend_on_alert_list(self):
        assert get_tool("alert_cancel").depends_on == ("alert_list",)
        assert get_tool("alert_pause").depends_on == ("alert_list",)
        assert get_tool("alert_resume").depends_on == ("alert_list",)

    def test_alert_create_refuses_no_entity_sentinel(self):
        """Regression test for the confirmed production bug: an unfiltered
        garbage entity ("AN"/"SET"/"ALSO") reaching this tool wrote a real
        alert row with a real threshold. NO_ENTITY_SENTINEL must never
        reach db_alert_add."""
        from graph.tool_catalogue import NO_ENTITY_SENTINEL

        with patch("graph.tool_catalogue.db_alert_add") as mock_add:
            result = get_tool("alert_create").fn(
                NO_ENTITY_SENTINEL,
                {"_query": "set an alert if it drops below 1400", "_user_id": 1},
            )
        mock_add.assert_not_called()
        assert result == {"action": "create", "symbol": NO_ENTITY_SENTINEL, "result": "invalid_symbol"}

    def test_alert_create_still_writes_for_a_real_entity(self):
        with patch("graph.tool_catalogue.db_alert_add", return_value=99) as mock_add:
            result = get_tool("alert_create").fn(
                "INFY",
                {"_query": "alert me if INFY drops below 1400", "_user_id": 1},
            )
        mock_add.assert_called_once()
        assert result["symbol"] == "INFY"
        assert result["result"] == "created"
        assert result["alert_id"] == 99


class TestScreenerToolAdapters:
    def test_screener_parse_query_reads_query_from_context(self):
        with patch("graph.tool_catalogue.screener_parse_query", return_value={"max_pe": 20}) as mock_fn:
            result = get_tool("screener_parse_query").fn("market", {"_query": "IT stocks PE below 20"})
        mock_fn.assert_called_once_with("IT stocks PE below 20")
        assert result == {"max_pe": 20}

    def test_screener_run_reads_filters_from_context(self):
        with patch("graph.tool_catalogue.run_screener", return_value=[{"symbol": "TCS"}]) as mock_fn:
            result = get_tool("screener_run").fn("market", {"screener_parse_query": {"max_pe": 20}})
        mock_fn.assert_called_once_with({"max_pe": 20})
        assert result == [{"symbol": "TCS"}]

    def test_screener_run_depends_on_parse_query(self):
        assert get_tool("screener_run").depends_on == ("screener_parse_query",)

    def test_screener_to_markdown_depends_on_screener_run(self):
        assert get_tool("screener_to_markdown").depends_on == ("screener_run",)

    def test_screener_to_markdown_reads_results_and_filters(self):
        with patch("graph.tool_catalogue.screener_to_markdown", return_value="| table |") as mock_fn:
            result = get_tool("screener_to_markdown").fn(
                "market",
                {
                    "screener_run": [{"symbol": "TCS"}],
                    "screener_parse_query": {"max_pe": 20},
                },
            )
        mock_fn.assert_called_once_with([{"symbol": "TCS"}], {"max_pe": 20})
        assert result == "| table |"

    def test_screener_save_parses_quoted_name_and_persists(self):
        with patch("graph.tool_catalogue.db_screener_save") as mock_save:
            result = get_tool("screener_save").fn(
                "market",
                {
                    "_query": 'save this screener as "My Value Picks"',
                    "_user_id": 9,
                    "screener_parse_query": {"max_pe": 20},
                },
            )
        mock_save.assert_called_once_with(9, "My Value Picks", {"max_pe": 20})
        assert result["name"] == "My Value Picks"

    def test_screener_save_defaults_name_when_unparseable(self):
        with patch("graph.tool_catalogue.db_screener_save"):
            result = get_tool("screener_save").fn("market", {"_query": "save it", "_user_id": 1})
        assert result["name"] == "My Screener"

    def test_fetch_top_movers_detects_loser_keyword(self):
        with patch("graph.tool_catalogue.get_top_movers", return_value="df") as mock_fn:
            get_tool("fetch_top_movers").fn("market", {"_query": "show me today's top losers"})
        mock_fn.assert_called_once_with("losers")

    def test_fetch_top_movers_defaults_to_gainers(self):
        with patch("graph.tool_catalogue.get_top_movers", return_value="df") as mock_fn:
            get_tool("fetch_top_movers").fn("market", {"_query": "top gainers today"})
        mock_fn.assert_called_once_with("gainers")

    def test_fetch_bulk_deals_scopes_to_real_ticker(self):
        with patch("graph.tool_catalogue.get_bulk_deals", return_value="df") as mock_fn:
            get_tool("fetch_bulk_deals").fn("TCS", {})
        mock_fn.assert_called_once_with("TCS")

    def test_fetch_bulk_deals_market_wide_for_sentinel_entity(self):
        with patch("graph.tool_catalogue.get_bulk_deals", return_value="df") as mock_fn:
            get_tool("fetch_bulk_deals").fn("market", {})
        mock_fn.assert_called_once_with(None)

    def test_fetch_block_deals_market_wide_for_sentinel_entity(self):
        with patch("graph.tool_catalogue.get_block_deals", return_value="df") as mock_fn:
            get_tool("fetch_block_deals").fn("market", {})
        mock_fn.assert_called_once_with(None)


class TestAlertSpecTools:
    """VAL-04: alert_create validates before writing; management by #id."""

    def _create(self, query, **patches):
        with patch("graph.tool_catalogue.db_alert_add", **patches) as add:
            out = get_tool("alert_create").fn("TCS", {"_query": query, "_user_id": 1})
        return out, add

    def test_already_true_asks_for_confirmation_instead_of_saving(self, monkeypatch):
        monkeypatch.setattr("graph.tool_catalogue._current_price", lambda s: 3400.0)
        out, add = self._create("alert me if TCS falls below 3500", return_value=9)
        add.assert_not_called()
        assert out["result"] == "needs_confirmation" and out["current_price"] == 3400.0

    def test_anyway_confirms_an_already_true_alert(self, monkeypatch):
        monkeypatch.setattr("graph.tool_catalogue._current_price", lambda s: 3400.0)
        out, add = self._create("alert me if TCS falls below 3500 anyway", return_value=9)
        assert out["result"] == "created" and add.call_args.kwargs["direction"] is None

    def test_out_of_range_is_refused_with_a_reason(self):
        out, add = self._create("alert if TCS RSI goes above 150", return_value=9)
        add.assert_not_called()
        assert out["result"] == "invalid_condition" and "0 to 100" in out["reason"]

    def test_direction_is_saved_for_percent_alerts(self):
        out, add = self._create("alert me if TCS drops 5%", return_value=9)
        assert out["result"] == "created"
        assert add.call_args.args[2] == "pct_change" and add.call_args.kwargs["direction"] == "down"

    def test_duplicate_and_cap_are_answered_not_raised(self):
        from utils.db import AlertLimitReached, DuplicateAlert

        out, _ = self._create("alert if TCS goes above 4000", side_effect=DuplicateAlert(3))
        assert (out["result"], out["alert_id"]) == ("duplicate", 3)
        out, _ = self._create("alert if TCS goes above 4000", side_effect=AlertLimitReached("50"))
        assert out["result"] == "limit_reached"

    def test_cancel_by_id_picks_that_alert_even_with_others_on_the_symbol(self):
        alerts = [{"id": 3, "symbol": "TCS", "active": True}, {"id": 7, "symbol": "TCS", "active": True}]
        ctx = {"_user_id": 1, "alert_list": alerts, "_query": "cancel alert #7"}
        with patch("graph.tool_catalogue.db_alert_remove") as rm:
            out = get_tool("alert_cancel").fn("market", ctx)
        rm.assert_called_once_with(1, 7)
        assert out["result"] == "removed" and out["symbol"] == "TCS"

    def test_cancel_by_someone_elses_id_is_not_found(self):
        alerts = [{"id": 3, "symbol": "TCS", "active": True}]
        ctx = {"_user_id": 1, "alert_list": alerts, "_query": "cancel alert #99"}
        with patch("graph.tool_catalogue.db_alert_remove") as rm:
            out = get_tool("alert_cancel").fn("market", ctx)
        rm.assert_not_called()
        assert out["result"] == "not_found"

    def test_ambiguous_lists_the_matching_ids(self):
        from graph.nodes.conclusion_builder import _alert_action_claim

        alerts = [
            {"id": 3, "symbol": "TCS", "active": True, "condition": "TCS: price goes above ₹4,000.00"},
            {"id": 7, "symbol": "TCS", "active": True, "condition": "TCS: RSI(14) crosses below 30"},
        ]
        ctx = {"_user_id": 1, "alert_list": alerts, "_query": "pause my TCS alert"}
        out = get_tool("alert_pause").fn("TCS", ctx)
        assert out["result"] == "ambiguous" and [m["id"] for m in out["matches"]] == [3, 7]
        claim = _alert_action_claim(out)
        assert "#3 price goes above ₹4,000.00" in claim and "#7 RSI(14) crosses below 30" in claim
