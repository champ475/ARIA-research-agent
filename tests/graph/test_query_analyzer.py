import json

import pytest

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
        stub = _StubClient(
            json.dumps({"intent": "equity", "objective": "analyse RELIANCE", "complexity": "moderate"})
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "what's the outlook on RELIANCE"})
        assert "RELIANCE" in result["entities"]

    def test_parses_intent_objective_complexity_from_llm_json(self):
        stub = _StubClient(
            json.dumps(
                {"intent": "equity", "objective": "analyse RELIANCE fundamentals", "complexity": "moderate"}
            )
        )
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

    def test_empty_llm_reply_keeps_the_users_words_as_objective(self):
        # Smoke test 2026-10-17: a reasoning model returned "" and the empty
        # objective made the judge flag every turn.
        node = build_query_analyzer_node(_StubClient(""))
        result = node({"query": "what about its debt?"})
        assert result["objective"] == "what about its debt?"

    def test_handles_markdown_fenced_json(self):
        stub = _StubClient(
            "```json\n"
            + json.dumps({"intent": "mf", "objective": "compare funds", "complexity": "simple"})
            + "\n```"
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "compare two mutual funds"})
        assert result["intent"] == "mf"

    def test_routes_through_its_node_model_route(self):
        """LLM-08: the node's client is built for its own model route (the
        fast class) instead of passing a Groq-only model name per call."""
        from unittest.mock import patch

        with patch("graph.nodes.query_analyzer.LLMClient") as client_cls:
            build_query_analyzer_node(None, provider_config=None)
        assert client_cls.call_args.kwargs["node"] == "query_analyzer"

    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "RELIANCE outlook"})
        assert len(result["research_journal"]) == 1
        assert result["research_journal"][0]["node"] == "QueryAnalyzer"


class TestQueryAnalyzerEntityFiltering:
    """Only mentions that resolve against the instrument master reach
    `entities` (VAL-02) — garbage tokens must never become Planner tasks."""

    def test_garbage_tokens_are_filtered_out(self):
        stub = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "secondary_intents": ["alert"],
                    "objective": "x",
                    "complexity": "complex",
                }
            )
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "Give me an IC report for INFY and also set an alert if it drops below 1400"})
        assert result["entities"] == ["INFY"]
        assert result["entity_resolutions"] == []

    def test_real_multi_entity_query_still_resolves_both(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "should I buy TCS or HDFC bank"})
        assert "TCS" in result["entities"]
        assert "HDFCBANK" in result["entities"]


class TestQueryAnalyzerMultiIntent:
    """Phase 7.4: secondary_intents lets one message name more than one domain."""

    def test_defaults_to_empty_secondary_intents(self):
        stub = _StubClient(json.dumps({"intent": "equity", "objective": "x", "complexity": "moderate"}))
        node = build_query_analyzer_node(stub)
        result = node({"query": "analyse TCS"})
        assert result["secondary_intents"] == []

    def test_parses_secondary_intents_list(self):
        stub = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "secondary_intents": ["alert"],
                    "objective": "report + alert",
                    "complexity": "complex",
                }
            )
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "generate a report on TCS and alert me if it drops 5%"})
        assert result["intent"] == "report"
        assert result["secondary_intents"] == ["alert"]

    def test_secondary_intents_never_duplicates_primary(self):
        stub = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "secondary_intents": ["report", "alert"],
                    "objective": "x",
                    "complexity": "complex",
                }
            )
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == ["alert"]

    def test_invalid_secondary_intent_labels_are_dropped(self):
        stub = _StubClient(
            json.dumps(
                {
                    "intent": "report",
                    "secondary_intents": ["alert", "not-a-real-domain"],
                    "objective": "x",
                    "complexity": "complex",
                }
            )
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == ["alert"]

    def test_non_list_secondary_intents_falls_back_to_empty(self):
        stub = _StubClient(
            json.dumps(
                {
                    "intent": "equity",
                    "secondary_intents": "alert",
                    "objective": "x",
                    "complexity": "moderate",
                }
            )
        )
        node = build_query_analyzer_node(stub)
        result = node({"query": "x"})
        assert result["secondary_intents"] == []


class TestAskingWhichCompany:
    """Smoke test 2026-10-17: questions with no company named ran tools on 'market'
    or answered with jargon; replies to 'which HDFC?' lost the original question."""

    @staticmethod
    def _clarification(question: str):
        from graph.entity_resolution import blocking_resolutions, clarification_text, resolve_mentions

        res = resolve_mentions(question)
        text = clarification_text(
            blocking_resolutions({"entity_resolutions": res.unresolved, "intent": "equity"})
        )
        return [{"role": "user", "content": question}, {"role": "assistant", "content": text}]

    def _analyzer(self, intent="equity", companies=()):
        return build_query_analyzer_node(
            _StubClient(
                json.dumps(
                    {
                        "intent": intent,
                        "objective": "obj",
                        "complexity": "moderate",
                        "companies": list(companies),
                    }
                )
            )
        )

    def test_reply_picks_one_option_and_keeps_the_original_question(self):
        history = [*self._clarification("How is HDFC doing?"), {"role": "user", "content": "the bank one"}]
        result = self._analyzer()({"query": "the bank one", "history": history})
        assert result["entities"] == ["HDFCBANK"]
        assert result["entity_resolutions"] == []
        assert "How is HDFC doing?" in result["query"] and "HDFCBANK" in result["query"]

    def test_reply_by_symbol(self):
        history = [*self._clarification("How is HDFC doing?"), {"role": "user", "content": "HDFCLIFE"}]
        result = self._analyzer()({"query": "HDFCLIFE", "history": history})
        assert result["entities"] == ["HDFCLIFE"]

    def test_reply_by_position(self):
        history = [*self._clarification("Tell me about Adani"), {"role": "user", "content": "the first one"}]
        result = self._analyzer(intent="chat")({"query": "the first one", "history": history})
        assert len(result["entities"]) == 1

    def test_a_long_new_question_is_not_taken_as_an_answer(self):
        question = "show me the technicals and the latest news for TCS please"
        history = [*self._clarification("How is HDFC doing?"), {"role": "user", "content": question}]
        result = self._analyzer()({"query": question, "history": history})
        assert result["entities"] == ["TCS"]
        assert result["query"] == question if "query" in result else True

    def test_unmatched_reply_stays_a_normal_turn(self):
        history = [*self._clarification("How is HDFC doing?"), {"role": "user", "content": "hmm not sure"}]
        result = self._analyzer()({"query": "hmm not sure", "history": history})
        assert "query" not in result

    def test_pointer_without_a_company_asks_which(self):
        from graph.entity_resolution import blocking_resolutions, clarification_text

        result = self._analyzer()({"query": "what about its debt?", "history": []})
        assert [u["status"] for u in result["entity_resolutions"]] == ["missing_company"]
        blocking = blocking_resolutions({**result, "intent": "equity"})
        assert "Which company do you mean?" in clarification_text(blocking)

    def test_pointer_after_a_company_turn_carries_over(self):
        history = [{"role": "user", "content": "How is TCS doing?"}, {"role": "assistant", "content": "ok"}]
        result = self._analyzer()({"query": "what about its debt?", "history": history})
        assert result["entities"] == ["TCS"]
        assert result["entity_resolutions"] == []

    def test_market_wide_and_portfolio_questions_do_not_ask(self):
        for q in ("Is the market good today?", "How is my portfolio doing?", "which stocks are gaining?"):
            result = self._analyzer()({"query": q, "history": []})
            assert result["entity_resolutions"] == [], q

    def test_bare_group_name_is_asked_even_when_called_chat(self):
        from graph.entity_resolution import blocking_resolutions

        result = self._analyzer(intent="chat")({"query": "Tell me about Tata", "history": []})
        blocking = blocking_resolutions({**result, "intent": "chat"})
        assert blocking and {c["symbol"] for c in blocking[0]["candidates"]} >= {"TCS", "TATASTEEL"}


class TestIntentCorrection:
    """Smoke test 2026-10-17: the fast model called a two-bank comparison a screen."""

    @staticmethod
    def _run(query, intent="screener"):
        stub = _StubClient(json.dumps({"intent": intent, "objective": "o", "complexity": "moderate"}))
        return build_query_analyzer_node(stub)({"query": query, "history": []})

    def test_named_companies_without_filters_is_equity(self):
        assert self._run("Compare HDFC Bank and ICICI Bank")["intent"] == "equity"

    def test_a_real_screen_with_a_named_company_stays_a_screen(self):
        assert self._run("IT stocks like TCS with PE under 25")["intent"] == "screener"

    def test_a_screen_naming_no_company_stays_a_screen(self):
        assert self._run("IT stocks with PE under 25 and ROE above 15%")["intent"] == "screener"

    def test_other_intents_are_left_alone(self):
        assert self._run("Compare HDFC Bank and ICICI Bank", intent="alert")["intent"] == "alert"


class TestIntentCorrectionByWords:
    """The 20b model labelled "PE of Infosys" mf and "How is TCS doing?" report (eval re-record 2026-10-18)."""

    @staticmethod
    def _run(query, intent):
        stub = _StubClient(json.dumps({"intent": intent, "objective": "o", "complexity": "moderate"}))
        return build_query_analyzer_node(stub)({"query": query, "history": []})["intent"]

    @pytest.mark.parametrize(
        "query", ["what's the PE of Infosys", "is Titan overvalued", "Bharti Airtel debt to equity"]
    )
    def test_company_question_labelled_mf_is_equity(self, query):
        assert self._run(query, "mf") == "equity"

    def test_a_real_fund_question_stays_mf(self):
        assert self._run("NAV of HDFC Flexi Cap fund for TCS holders", "mf") == "mf"
        assert (
            self._run("Compare Parag Parikh Flexi Cap and HDFC Flexi Cap", "mf") == "mf"
        )  # no company named

    @pytest.mark.parametrize("query", ["How is TCS doing?", "latest news on ITC"])
    def test_quote_question_labelled_report_is_equity(self, query):
        assert self._run(query, "report") == "equity"

    @pytest.mark.parametrize(
        "query",
        [
            "Give me an IC report on TCS",
            "quarterly results summary for Wipro",
            "research note Tata Steel",
            "initiate coverage on HDFC Bank",
            "deep dive on Bajaj Finance",
        ],
    )
    def test_report_requests_stay_report(self, query):
        assert self._run(query, "report") == "report"
