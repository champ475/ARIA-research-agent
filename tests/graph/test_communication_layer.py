from unittest.mock import patch

import pandas as pd

from graph.nodes.communication_layer import build_communication_layer_node


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.last_call_kwargs = None
        self.last_prompt = None

    def ask(self, prompt, **kwargs):
        self.last_prompt = prompt
        self.last_call_kwargs = kwargs
        return self._response


def _conclusion(**overrides):
    base = {
        "claim": "RELIANCE pe = 24.3",
        "value": 24.3,
        "confidence": 0.9,
        "evidence_ids": ["ev-1"],
        "citation_key": "pe",
    }
    base.update(overrides)
    return base


class TestCommunicationLayerNoConclusions:
    def test_reports_inability_to_answer_without_calling_llm(self):
        stub = _StubClient("should not be used")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [], "objective": "analyse RELIANCE", "entities": [], "evidence": [],
            "complexity": "moderate",
        })
        assert "wasn't able to verify" in result["final_report"]
        assert stub.last_prompt is None

    def test_confidence_summary_empty(self):
        stub = _StubClient("x")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [], "objective": "x", "entities": [], "evidence": [], "complexity": "moderate"})
        assert result["confidence_summary"] == {}

    def test_trivial_query_answered_directly_not_reported_as_a_gap(self):
        # intent defaults to "equity" (missing "intent" key), a streamable
        # domain — the LLM call is deferred to GraphOrchestrator instead of
        # made synchronously here, see _is_streamable.
        stub = _StubClient("PE stands for price-to-earnings ratio.")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [], "objective": "explain PE ratio", "entities": [], "evidence": [],
            "complexity": "simple", "query": "what does PE mean",
        })
        assert result["final_report"] == ""
        assert result["pending_synthesis_prompt"] == "what does PE mean"
        assert stub.last_prompt is None

    def test_non_streamable_trivial_query_answered_directly(self):
        stub = _StubClient("PE stands for price-to-earnings ratio.")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [], "objective": "explain PE ratio", "entities": [], "evidence": [],
            "complexity": "simple", "query": "what does PE mean", "intent": "mf",
        })
        assert result["final_report"] == "PE stands for price-to-earnings ratio."
        assert stub.last_prompt is not None


class TestCommunicationLayerReport:
    """intent="mf" throughout — any non-streamable domain (see
    graph/nodes/communication_layer.py::_is_streamable) forces the
    synchronous .ask() path these tests are actually exercising; without an
    explicit intent it defaults to "equity", which now defers the call."""

    def test_prompt_includes_every_conclusion_claim(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        conclusions = [_conclusion(claim="RELIANCE pe = 24.3"), _conclusion(claim="RELIANCE roce = 15.2", citation_key="roce")]
        node({"conclusions": conclusions, "objective": "x", "entities": ["RELIANCE"], "evidence": [], "intent": "mf"})
        assert "RELIANCE pe = 24.3" in stub.last_prompt
        assert "RELIANCE roce = 15.2" in stub.last_prompt

    def test_canonical_citation_key_gets_footnote_marker_in_prompt(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion(citation_key="pe")], "objective": "x", "entities": [], "evidence": [], "intent": "mf"})
        assert "[^pe]" in stub.last_prompt

    def test_non_canonical_citation_key_gets_no_marker(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion(citation_key="RELIANCE.weird_metric")], "objective": "x", "entities": [], "evidence": [], "intent": "mf"})
        assert "[^" not in stub.last_prompt

    def test_confidence_summary_maps_citation_key_to_confidence(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [_conclusion(citation_key="pe", confidence=0.9), _conclusion(citation_key="roce", confidence=0.6)],
            "objective": "x", "entities": [], "evidence": [], "intent": "mf",
        })
        assert result["confidence_summary"] == {"pe": 0.9, "roce": 0.6}

    def test_uses_primary_model_no_override(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion()], "objective": "x", "entities": [], "evidence": [], "intent": "mf"})
        assert "model" not in stub.last_call_kwargs

    def test_writes_journal_entry(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [_conclusion()], "objective": "x", "entities": [], "evidence": [], "intent": "mf"})
        assert result["research_journal"][0]["node"] == "CommunicationLayer"


class TestCommunicationLayerFootnotes:
    def test_appends_cited_footnotes_when_fundamental_evidence_present(self):
        stub = _StubClient("P/E of 24.3 [^pe]")
        fundamental_ev = {
            "id": "ev-fund", "tool": "compute_fundamentals", "entity": "RELIANCE",
            "evidence_type": "fundamental_ratio", "payload": {"pe": 24.3, "pe_detail": "24.3 = price/eps"},
            "source": "compute_fundamentals", "timestamp": None, "confidence": 0.9, "citation_key": "pe",
        }
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [_conclusion()], "objective": "x", "entities": ["RELIANCE"],
            "evidence": [fundamental_ev], "intent": "mf",
        })
        assert "Cited calculations" in result["final_report"]
        assert "24.3 = price/eps" in result["final_report"]

    def test_no_footnotes_appended_without_fundamental_evidence(self):
        stub = _StubClient("Report text with no ratios")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [_conclusion()], "objective": "x", "entities": ["RELIANCE"], "evidence": [], "intent": "mf"})
        assert result["final_report"] == "Report text with no ratios"


class TestCommunicationLayerPlanning:
    """Phase 7.3: planning intent bypasses the Evidence/Conclusion pipeline
    entirely — see module docstring for why. All assertions patch
    utils.planning_logic's intake_turn/build_strategy and utils.db's
    profile_get/upsert/clear at their communication_layer import site."""

    def test_missing_user_id_returns_early_without_touching_db(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.profile_get") as mock_get:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "I'm 30 with 10 lakhs", "conclusions": []})
        mock_get.assert_not_called()
        assert "sign in" in result["final_report"]

    def test_show_profile_keyword_renders_existing_profile(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.profile_get", return_value={"age": 30, "capital": 1000000}):
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "show my profile", "user_id": 1, "conclusions": []})
        assert "Age" in result["final_report"]
        assert "30" in result["final_report"]

    def test_reset_keyword_clears_profile(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.profile_clear") as mock_clear:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "reset my profile", "user_id": 1, "conclusions": []})
        mock_clear.assert_called_once_with(1)
        assert "cleared" in result["final_report"]

    def test_incomplete_intake_returns_reply_without_building_strategy(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.profile_get", return_value={}), \
             patch("graph.nodes.communication_layer._planning_intake_turn", return_value=({"age": 30}, False, "Tell me more")), \
             patch("graph.nodes.communication_layer.profile_upsert", return_value={"age": 30}) as mock_upsert, \
             patch("graph.nodes.communication_layer._planning_build_strategy") as mock_build:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "I'm 30", "user_id": 1, "conclusions": []})
        mock_upsert.assert_called_once_with(1, {"age": 30})
        mock_build.assert_not_called()
        assert result["final_report"] == "Tell me more"

    def test_complete_intake_triggers_strategy_build(self):
        stub = _StubClient("unused")
        complete_profile = {"age": 30, "capital": 1000000, "horizon_years": 20, "goal": "retirement", "risk_tolerance": "moderate"}
        with patch("graph.nodes.communication_layer.profile_get", return_value={}), \
             patch("graph.nodes.communication_layer._planning_intake_turn", return_value=(complete_profile, True, "Great, building now")), \
             patch("graph.nodes.communication_layer.profile_upsert", return_value=complete_profile), \
             patch("graph.nodes.communication_layer._planning_build_strategy", return_value="Full strategy text") as mock_build:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "here's everything", "user_id": 1, "conclusions": []})
        mock_build.assert_called_once_with(stub, complete_profile)
        assert result["final_report"] == "Full strategy text"

    def test_already_complete_profile_without_build_trigger_just_replies(self):
        stub = _StubClient("unused")
        complete_profile = {"age": 30, "capital": 1000000, "horizon_years": 20, "goal": "retirement", "risk_tolerance": "moderate"}
        with patch("graph.nodes.communication_layer.profile_get", return_value=complete_profile), \
             patch("graph.nodes.communication_layer._planning_intake_turn", return_value=({}, True, "Anything else?")), \
             patch("graph.nodes.communication_layer._planning_build_strategy") as mock_build:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "ok", "user_id": 1, "conclusions": []})
        mock_build.assert_not_called()
        assert result["final_report"] == "Anything else?"

    def test_already_complete_profile_with_explicit_build_request_builds(self):
        stub = _StubClient("unused")
        complete_profile = {"age": 30, "capital": 1000000, "horizon_years": 20, "goal": "retirement", "risk_tolerance": "moderate"}
        with patch("graph.nodes.communication_layer.profile_get", return_value=complete_profile), \
             patch("graph.nodes.communication_layer._planning_intake_turn", return_value=({}, True, "reply")), \
             patch("graph.nodes.communication_layer._planning_build_strategy", return_value="strategy") as mock_build:
            node = build_communication_layer_node(stub)
            result = node({"intent": "planning", "query": "go ahead and build it", "user_id": 1, "conclusions": []})
        mock_build.assert_called_once()
        assert result["final_report"] == "strategy"

    def test_planning_never_calls_the_generic_conclusion_prompt(self):
        stub = _StubClient("should not be used")
        with patch("graph.nodes.communication_layer.profile_get", return_value={}), \
             patch("graph.nodes.communication_layer._planning_intake_turn", return_value=({}, False, "reply")):
            node = build_communication_layer_node(stub)
            node({"intent": "planning", "query": "hi", "user_id": 1, "conclusions": [_conclusion()]})
        assert stub.last_prompt is None


class TestCommunicationLayerChart:
    def test_renders_chart_when_ohlcv_evidence_present(self):
        stub = _StubClient("Report text")
        df = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]})
        ohlcv_ev = {
            "id": "ev-ohlcv", "tool": "fetch_ohlcv", "entity": "RELIANCE",
            "evidence_type": "price_ohlcv", "payload": df,
            "source": "fetch_ohlcv", "timestamp": None, "confidence": 1.0, "citation_key": "price_ohlcv",
        }
        with patch("graph.nodes.communication_layer.candlestick", return_value="fake-figure") as mock_chart:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "entities": ["RELIANCE"],
                "evidence": [ohlcv_ev],
            })
        mock_chart.assert_called_once()
        assert result["chart_html"] == "fake-figure"

    def test_no_chart_without_ohlcv_evidence(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [_conclusion()], "objective": "x", "entities": ["RELIANCE"], "evidence": []})
        assert result["chart_html"] is None


class TestCommunicationLayerReportFiles:
    """Phase 7.1: report-domain queries write PDF/MD files and persist to the
    `reports` table from CommunicationLayer (final_report only exists here —
    see module docstring for why this isn't in ConclusionBuilder)."""

    def test_report_intent_writes_files_and_persists(self):
        stub = _StubClient("Full IC report body")
        with patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/x.md", "/tmp/x.pdf")) as mock_save, \
             patch("graph.nodes.communication_layer.report_save") as mock_persist:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "IC report on RELIANCE",
                "query": "generate an IC report on RELIANCE", "entities": ["RELIANCE"], "evidence": [],
                "intent": "report", "user_id": 42,
            })
        mock_save.assert_called_once_with("RELIANCE", "IC", "Full IC report body")
        mock_persist.assert_called_once_with(42, "RELIANCE", "IC", "IC — RELIANCE", "Full IC report body")
        assert result["md_path"] == "/tmp/x.md"
        assert result["pdf_path"] == "/tmp/x.pdf"

    def test_report_intent_detects_result_update_type(self):
        stub = _StubClient("RU report body")
        with patch("graph.nodes.communication_layer.render_report", return_value=(None, None)) as mock_save, \
             patch("graph.nodes.communication_layer.report_save"):
            node = build_communication_layer_node(stub)
            node({
                "conclusions": [_conclusion()], "objective": "x",
                "query": "quarterly results update for TCS", "entities": ["TCS"], "evidence": [],
                "intent": "report", "user_id": 1,
            })
        assert mock_save.call_args[0][1] == "RU"

    def test_non_report_intent_never_writes_files(self):
        stub = _StubClient("Equity answer")
        with patch("graph.nodes.communication_layer.render_report") as mock_save, \
             patch("graph.nodes.communication_layer.report_save") as mock_persist:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "what's TCS's PE",
                "entities": ["TCS"], "evidence": [], "intent": "equity", "user_id": 1,
            })
        mock_save.assert_not_called()
        mock_persist.assert_not_called()
        assert result["pdf_path"] is None
        assert result["md_path"] is None

    def test_screener_intent_writes_csv_from_raw_evidence(self):
        stub = _StubClient("Here are the matching stocks.")
        screener_ev = {
            "id": "ev-run", "tool": "screener_run", "entity": "market",
            "evidence_type": "screener_results", "payload": [{"symbol": "TCS", "pe": 24.3}],
            "source": "screener_run", "timestamp": None, "confidence": 1.0, "citation_key": "screener_results",
        }
        with patch("graph.nodes.communication_layer.save_screener_csv", return_value="/tmp/s.csv") as mock_csv:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "IT stocks PE below 25",
                "entities": [], "evidence": [screener_ev], "intent": "screener",
            })
        mock_csv.assert_called_once_with([{"symbol": "TCS", "pe": 24.3}])
        assert result["csv_path"] == "/tmp/s.csv"

    def test_non_screener_intent_never_writes_csv(self):
        stub = _StubClient("Equity answer")
        with patch("graph.nodes.communication_layer.save_screener_csv") as mock_csv:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "what's TCS's PE",
                "entities": ["TCS"], "evidence": [], "intent": "equity",
            })
        mock_csv.assert_not_called()
        assert result["csv_path"] is None

    def test_screener_intent_with_no_screener_run_evidence_writes_no_csv(self):
        stub = _StubClient("No results.")
        with patch("graph.nodes.communication_layer.save_screener_csv") as mock_csv:
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "screen for value stocks",
                "entities": [], "evidence": [], "intent": "screener",
            })
        mock_csv.assert_not_called()
        assert result["csv_path"] is None

    def test_secondary_intent_report_still_writes_files(self):
        # Phase 7.4: primary intent is "alert" but "report" is a secondary
        # intent — the report branch must still fire.
        stub = _StubClient("Full report body")
        with patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/x.md", "/tmp/x.pdf")) as mock_save, \
             patch("graph.nodes.communication_layer.report_save"):
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "report on RELIANCE and alert me",
                "entities": ["RELIANCE"], "evidence": [], "intent": "alert", "secondary_intents": ["report"], "user_id": 1,
            })
        mock_save.assert_called_once()
        assert result["md_path"] == "/tmp/x.md"

    def test_no_secondary_intents_key_does_not_crash(self):
        stub = _StubClient("Equity answer")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [_conclusion()], "objective": "x", "query": "what's TCS's PE",
            "entities": ["TCS"], "evidence": [], "intent": "equity",
        })
        assert result["pdf_path"] is None

    def test_persistence_failure_does_not_block_report_return(self):
        stub = _StubClient("Report body")
        with patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/x.md", "/tmp/x.pdf")), \
             patch("graph.nodes.communication_layer.report_save", side_effect=RuntimeError("db down")):
            node = build_communication_layer_node(stub)
            result = node({
                "conclusions": [_conclusion()], "objective": "x", "query": "IC report on RELIANCE",
                "entities": ["RELIANCE"], "evidence": [], "intent": "report", "user_id": 1,
            })
        assert result["final_report"] == "Report body"
        assert result["md_path"] == "/tmp/x.md"


class TestCommunicationLayerReportManagement:
    """Phase 3 of the open-source restructure: "show my reports" / "download
    report #N" bypass Evidence/Conclusions entirely (_handle_report_management),
    same pattern as planning's bypass — a DB read/render, not a research task."""

    def test_missing_user_id_returns_early(self):
        stub = _StubClient("should not be used")
        node = build_communication_layer_node(stub)
        result = node({"intent": "report", "query": "show my reports", "conclusions": []})
        assert "sign in" in result["final_report"]
        assert stub.last_prompt is None

    def test_list_reports_renders_table(self):
        stub = _StubClient("unused")
        reports = [
            {"id": 3, "report_type": "IC", "title": "IC — TCS", "created_at": __import__("datetime").datetime(2026, 1, 5)},
        ]
        with patch("graph.nodes.communication_layer.report_list", return_value=reports):
            node = build_communication_layer_node(stub)
            result = node({"intent": "report", "query": "show my reports", "conclusions": [], "user_id": 1})
        assert "IC — TCS" in result["final_report"]
        assert "2026-01-05" in result["final_report"]
        assert result["pdf_path"] is None  # listing never renders files
        assert stub.last_prompt is None

    def test_list_reports_empty_state(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.report_list", return_value=[]):
            node = build_communication_layer_node(stub)
            result = node({"intent": "report", "query": "list my reports", "conclusions": [], "user_id": 1})
        assert "no saved reports" in result["final_report"]

    def test_download_report_renders_from_stored_content(self):
        stub = _StubClient("unused")
        row = {"id": 3, "symbol": "TCS", "report_type": "IC", "title": "IC — TCS", "content": "Full report text"}
        with patch("graph.nodes.communication_layer.report_get", return_value=row) as mock_get, \
             patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/x.md", "/tmp/x.pdf")) as mock_render:
            node = build_communication_layer_node(stub)
            result = node({"intent": "report", "query": "download report #3", "conclusions": [], "user_id": 1})
        mock_get.assert_called_once_with(1, 3)
        mock_render.assert_called_once_with("TCS", "IC", "Full report text")
        assert result["md_path"] == "/tmp/x.md"
        assert result["pdf_path"] == "/tmp/x.pdf"
        assert "IC — TCS" in result["final_report"]
        assert stub.last_prompt is None

    def test_download_report_not_found(self):
        stub = _StubClient("unused")
        with patch("graph.nodes.communication_layer.report_get", return_value=None):
            node = build_communication_layer_node(stub)
            result = node({"intent": "report", "query": "download report #99", "conclusions": [], "user_id": 1})
        assert "Couldn't find report #99" in result["final_report"]
        assert result["pdf_path"] is None

    def test_download_without_report_id_falls_through_to_generation(self):
        # "download" keyword present but no number — can't tell which report,
        # so this must NOT be treated as a management action; it falls
        # through to normal report generation further down in the function.
        stub = _StubClient("Freshly generated report")
        with patch("graph.nodes.communication_layer.render_report", return_value=(None, None)), \
             patch("graph.nodes.communication_layer.report_save"):
            node = build_communication_layer_node(stub)
            result = node({
                "intent": "report", "query": "download a report on TCS", "conclusions": [_conclusion()],
                "objective": "x", "entities": ["TCS"], "evidence": [], "user_id": 1,
            })
        assert result["final_report"] == "Freshly generated report"

    def test_plain_report_generation_query_is_unaffected(self):
        stub = _StubClient("Full IC report body")
        with patch("graph.nodes.communication_layer.render_report", return_value=("/tmp/x.md", "/tmp/x.pdf")), \
             patch("graph.nodes.communication_layer.report_save"):
            node = build_communication_layer_node(stub)
            result = node({
                "intent": "report", "query": "generate an IC report on RELIANCE", "conclusions": [_conclusion()],
                "objective": "IC report on RELIANCE", "entities": ["RELIANCE"], "evidence": [], "user_id": 1,
            })
        assert result["final_report"] == "Full IC report body"
