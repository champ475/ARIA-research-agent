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
        result = node({"conclusions": [], "objective": "analyse RELIANCE", "entities": [], "evidence": []})
        assert "wasn't able to verify" in result["final_report"]
        assert stub.last_prompt is None

    def test_confidence_summary_empty(self):
        stub = _StubClient("x")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [], "objective": "x", "entities": [], "evidence": []})
        assert result["confidence_summary"] == {}


class TestCommunicationLayerReport:
    def test_prompt_includes_every_conclusion_claim(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        conclusions = [_conclusion(claim="RELIANCE pe = 24.3"), _conclusion(claim="RELIANCE roce = 15.2", citation_key="roce")]
        node({"conclusions": conclusions, "objective": "x", "entities": ["RELIANCE"], "evidence": []})
        assert "RELIANCE pe = 24.3" in stub.last_prompt
        assert "RELIANCE roce = 15.2" in stub.last_prompt

    def test_canonical_citation_key_gets_footnote_marker_in_prompt(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion(citation_key="pe")], "objective": "x", "entities": [], "evidence": []})
        assert "[^pe]" in stub.last_prompt

    def test_non_canonical_citation_key_gets_no_marker(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion(citation_key="RELIANCE.weird_metric")], "objective": "x", "entities": [], "evidence": []})
        assert "[^" not in stub.last_prompt

    def test_confidence_summary_maps_citation_key_to_confidence(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        result = node({
            "conclusions": [_conclusion(citation_key="pe", confidence=0.9), _conclusion(citation_key="roce", confidence=0.6)],
            "objective": "x", "entities": [], "evidence": [],
        })
        assert result["confidence_summary"] == {"pe": 0.9, "roce": 0.6}

    def test_uses_primary_model_no_override(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        node({"conclusions": [_conclusion()], "objective": "x", "entities": [], "evidence": []})
        assert "model" not in stub.last_call_kwargs

    def test_writes_journal_entry(self):
        stub = _StubClient("Report text")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [_conclusion()], "objective": "x", "entities": [], "evidence": []})
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
            "evidence": [fundamental_ev],
        })
        assert "Cited calculations" in result["final_report"]
        assert "24.3 = price/eps" in result["final_report"]

    def test_no_footnotes_appended_without_fundamental_evidence(self):
        stub = _StubClient("Report text with no ratios")
        node = build_communication_layer_node(stub)
        result = node({"conclusions": [_conclusion()], "objective": "x", "entities": ["RELIANCE"], "evidence": []})
        assert result["final_report"] == "Report text with no ratios"


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
