import json

from graph.nodes.query_analyzer import build_query_analyzer_node


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.last_call_kwargs = None

    def ask(self, prompt, **kwargs):
        self.last_call_kwargs = kwargs
        return self._response


class TestQueryAnalyzer:
    def test_extracts_entities_deterministically(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "analyse RELIANCE", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "what's the outlook on RELIANCE"})
        assert "RELIANCE" in result["entities"]

    def test_parses_intent_objective_complexity_from_llm_json(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "how are RELIANCE fundamentals looking"})
        assert result["intent"] == "equity"
        assert result["objective"] == "analyse RELIANCE fundamentals"
        assert result["complexity"] == "moderate"

    def test_falls_back_on_unparseable_llm_output(self):
        stub = _StubClient("not json at all")
        node = build_query_analyzer_node(stub)
        result = node({"query": "hello"})
        assert result["intent"] == "chat"
        assert result["complexity"] == "simple"

    def test_handles_markdown_fenced_json(self):
        stub = _StubClient("```json\n" + json.dumps({"intent": "mf", "objective": "compare funds", "complexity": "simple"}) + "\n```")
        node = build_query_analyzer_node(stub)
        result = node({"query": "compare two mutual funds"})
        assert result["intent"] == "mf"

    def test_routes_through_fast_model(self):
        from config import GROQ_MODEL_FAST
        stub = _StubClient(json.dumps({"intent": "chat", "objective": "", "complexity": "simple"}))
        node = build_query_analyzer_node(stub)
        node({"query": "hi"})
        assert stub.last_call_kwargs["model"] == GROQ_MODEL_FAST

    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "RELIANCE outlook"})
        assert len(result["research_journal"]) == 1
        assert result["research_journal"][0]["node"] == "QueryAnalyzer"


class TestQueryAnalyzerMultiIntent:
    """Phase 7.4: secondary_intents lets one message name more than one domain."""

    def test_defaults_to_empty_secondary_intents(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "analyse TCS"})
        assert result["secondary_intents"] == []

    def test_parses_secondary_intents_list(self):
        stub = _StubClient(json.dumps({
            "intent": "report", "secondary_intents": ["alert"],
            "objective": "report + alert", "complexity": "complex",
        }))
        node = build_query_analyzer_node(stub)
        result = node({"query": "generate a report on TCS and alert me if it drops 5%"})
        assert result["intent"] == "report"
        assert result["secondary_intents"] == ["alert"]

    def test_secondary_intents_never_duplicates_primary(self):
        stub = _StubClient(json.dumps({
            "intent": "report", "secondary_intents": ["report", "alert"],
            "objective": "x", "complexity": "complex",
        }))
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == ["alert"]

    def test_invalid_secondary_intent_labels_are_dropped(self):
        stub = _StubClient(json.dumps({
            "intent": "report", "secondary_intents": ["alert", "not-a-real-domain"],
            "objective": "x", "complexity": "complex",
        }))
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == ["alert"]

    def test_non_list_secondary_intents_falls_back_to_empty(self):
        stub = _StubClient(json.dumps({
            "intent": "equity", "secondary_intents": "alert", "objective": "x", "complexity": "moderate",
        }))
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == []
