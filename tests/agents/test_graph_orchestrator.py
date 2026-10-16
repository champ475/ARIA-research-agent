import contextlib
from unittest.mock import MagicMock, patch

import pytest

from agents.graph_orchestrator import GraphOrchestrator


def _build_orchestrator(fake_graph):
    """Patches build_graph/get_postgres_checkpointer for construction (which
    resolves a BYO config once, see _resolve_byo_config), then monkey-patches
    the instance's _trial_quota_exceeded so every test in this module stays
    DB-free (trial path, never blocked) without needing its own patch —
    process_stream() re-checks this every fresh turn, so it must survive
    past the `with` block construction happens in."""
    with (
        patch("agents.graph_orchestrator.shared_graph", return_value=fake_graph),
        patch("agents.graph_orchestrator.get_postgres_checkpointer", return_value=None),
        patch.object(GraphOrchestrator, "_resolve_byo_config", return_value=None),
    ):
        orch = GraphOrchestrator(user_id=1)
    orch._trial_quota_exceeded = lambda: False
    return orch


class TestGraphOrchestratorNormalFlow:
    def test_process_stream_yields_final_report_and_chart(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "RELIANCE looks strong.", "chart_html": "fig"}
        orch = _build_orchestrator(fake_graph)

        results = list(orch.process_stream("analyse RELIANCE"))

        assert len(results) == 1
        text, chart, pdf, md, csv, tab = results[0]
        assert text == "RELIANCE looks strong."
        assert chart == "fig"
        assert tab == "equity"

    def test_process_drains_stream_and_returns_last(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "Report.", "chart_html": None}
        orch = _build_orchestrator(fake_graph)

        result = orch.process("analyse RELIANCE")

        assert result[0] == "Report."

    def test_invoke_called_with_thread_id_and_query(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None}
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("analyse RELIANCE"))  # generator, must iterate

        args, kwargs = fake_graph.invoke.call_args
        assert args[0]["query"] == "analyse RELIANCE"
        # Random UUID component, not a deterministic user+turn counter — see
        # agents/graph_orchestrator.py's "Thread-id scoping" docstring for
        # why the counter-based scheme was unsafe across process restarts.
        assert kwargs["config"]["configurable"]["thread_id"].startswith("user-1-")
        assert kwargs["config"]["configurable"]["thread_id"] != "user-1-"


class TestGraphOrchestratorHumanInTheLoop:
    def test_interrupt_surfaces_prompt_as_response_text(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed with 6 tasks?"}
        fake_graph.invoke.return_value = {"__interrupt__": [fake_interrupt]}
        orch = _build_orchestrator(fake_graph)

        text, chart, pdf, md, csv, tab = list(orch.process_stream("compare six stocks"))[0]

        assert "Proceed with 6 tasks?" in text
        assert "yes/no" in text

    def test_sets_awaiting_confirmation_after_interrupt(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.return_value = {"__interrupt__": [fake_interrupt]}
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("compare six stocks"))

        assert orch._awaiting_confirmation is True

    def test_next_message_resumes_with_approval(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.side_effect = [
            {"__interrupt__": [fake_interrupt]},
            {"final_report": "Comparison complete.", "chart_html": None},
        ]
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("compare six stocks"))
        result = list(orch.process_stream("yes"))[0]

        assert result[0] == "Comparison complete."
        from langgraph.types import Command

        _, kwargs = fake_graph.invoke.call_args_list[1]
        resume_arg = fake_graph.invoke.call_args_list[1][0][0]
        assert isinstance(resume_arg, Command)
        assert resume_arg.resume is True

    def test_next_message_resumes_with_decline(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.side_effect = [
            {"__interrupt__": [fake_interrupt]},
            {"final_report": "You declined.", "chart_html": None},
        ]
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("compare six stocks"))
        list(orch.process_stream("no thanks"))

        resume_arg = fake_graph.invoke.call_args_list[1][0][0]
        assert resume_arg.resume is False

    def test_awaiting_confirmation_cleared_after_resume(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.side_effect = [
            {"__interrupt__": [fake_interrupt]},
            {"final_report": "Done.", "chart_html": None},
        ]
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("compare six stocks"))
        list(orch.process_stream("yes"))

        assert orch._awaiting_confirmation is False


class TestGraphOrchestratorThreadIdScoping:
    """Phase 7.4: every fresh (non-resume) turn gets a new thread_id, so
    reducer-backed GraphState fields (tasks/evidence/research_journal) from
    a prior turn can never leak into the next one's Task list."""

    def test_each_fresh_turn_gets_a_different_thread_id(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None}
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("first message"))
        first_thread = fake_graph.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]
        list(orch.process_stream("second message"))
        second_thread = fake_graph.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]

        assert first_thread != second_thread

    def test_resume_reuses_the_same_thread_id_as_the_interrupted_turn(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.side_effect = [
            {"__interrupt__": [fake_interrupt]},
            {"final_report": "Done.", "chart_html": None},
        ]
        orch = _build_orchestrator(fake_graph)

        list(orch.process_stream("compare six stocks"))
        interrupted_thread = fake_graph.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]
        list(orch.process_stream("yes"))
        resume_thread = fake_graph.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]

        assert interrupted_thread == resume_thread

    def test_reset_starts_a_fresh_thread_id(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None}
        orch = _build_orchestrator(fake_graph)
        list(orch.process_stream("first message"))
        before = orch._thread_id

        orch.reset()

        assert orch._thread_id != before

    def test_load_history_does_not_raise_and_resets_thread(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None}
        orch = _build_orchestrator(fake_graph)
        list(orch.process_stream("advance the turn counter first"))
        before = orch._thread_id

        orch.load_history([{"role": "user", "content": "hi"}])  # must not raise AttributeError

        assert orch._thread_id != before
        assert orch._awaiting_confirmation is False


class TestGraphOrchestratorTabUsed:
    """Phase 7.4: tab_used derives from intent + secondary_intents rather
    than being hardcoded "equity", so the routing badge reflects multi-domain
    turns without changing the 6-tuple yield contract."""

    def test_single_intent_yields_bare_domain(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None, "intent": "mf"}
        orch = _build_orchestrator(fake_graph)
        _, _, _, _, _, tab = list(orch.process_stream("compare funds"))[0]
        assert tab == "mf"

    def test_secondary_intents_joined_with_plus(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {
            "final_report": "x",
            "chart_html": None,
            "intent": "report",
            "secondary_intents": ["alert"],
        }
        orch = _build_orchestrator(fake_graph)
        _, _, _, _, _, tab = list(orch.process_stream("report + alert"))[0]
        assert tab == "report+alert"

    def test_missing_intent_defaults_to_equity(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x", "chart_html": None}
        orch = _build_orchestrator(fake_graph)
        _, _, _, _, _, tab = list(orch.process_stream("hi"))[0]
        assert tab == "equity"


class TestGraphOrchestratorClassify:
    """Phase 7.4: app.py:102's timeframe-keyword pre-check calls
    classify() on whichever orchestrator is active, so its decision can
    never disagree with the active orchestrator's own routing."""

    def test_classify_delegates_to_keyword_route(self):
        orch = _build_orchestrator(MagicMock())
        with patch("agents.graph_orchestrator._keyword_route", return_value="mf") as mock_route:
            result = orch.classify("what's the NAV of this fund")
        mock_route.assert_called_once_with("what's the NAV of this fund")
        assert result == "mf"

    def test_classify_does_not_invoke_the_graph(self):
        fake_graph = MagicMock()
        orch = _build_orchestrator(fake_graph)
        orch.classify("analyse RELIANCE")
        fake_graph.invoke.assert_not_called()


class TestGraphOrchestratorResolveByoConfig:
    """Bug fix: a decrypt() failure (e.g. SECRET_KEY unset/changed since a
    BYO key was saved) must fall back to the trial path, not crash
    orchestrator construction — same graceful-degrade philosophy as the
    rest of __init__."""

    def test_decrypt_failure_falls_back_to_trial_path(self):
        fake_row = {
            "provider": "openai_compatible",
            "base_url": None,
            "model": "gpt-4o-mini",
            "api_key_encrypted": "bogus-ciphertext",
        }
        with (
            patch("agents.graph_orchestrator.build_graph", return_value=MagicMock()),
            patch("agents.graph_orchestrator.get_postgres_checkpointer", return_value=None),
            patch("utils.db.llm_config_get", return_value=fake_row),
            patch("utils.crypto.decrypt", side_effect=ValueError("Stored API key could not be decrypted")),
        ):
            orch = GraphOrchestrator(user_id=1)  # must not raise
        assert orch._provider_config is None

    def test_valid_byo_config_resolves_normally(self):
        fake_row = {
            "provider": "openai_compatible",
            "base_url": "https://api.example.com",
            "model": "gpt-4o-mini",
            "api_key_encrypted": "ciphertext",
        }
        with (
            patch("agents.graph_orchestrator.build_graph", return_value=MagicMock()),
            patch("agents.graph_orchestrator.get_postgres_checkpointer", return_value=None),
            patch("utils.db.llm_config_get", return_value=fake_row),
            patch("utils.crypto.decrypt", return_value="sk-real-key"),
        ):
            orch = GraphOrchestrator(user_id=1)
        assert orch._provider_config == {
            "provider": "openai_compatible",
            "base_url": "https://api.example.com",
            "model": "gpt-4o-mini",
            "api_key": "sk-real-key",
        }


class TestGraphOrchestratorMisc:
    def test_generate_suggestions_parses_llm_json_array(self):
        # LLM chips are a BYO-user feature since LLM-11 (trial chips are deterministic).
        orch = _build_orchestrator(MagicMock())
        orch._provider_config = {"api_key": "k", "model": "m"}
        fake_client = MagicMock()
        fake_client.ask.return_value = (
            '["Show my alerts", "Compare TCS vs INFY", "Top gainers today", "Set a price alert"]'
        )
        with patch("agents.llm_client.LLMClient", return_value=fake_client):
            result = orch.generate_suggestions("resp", "query")
        assert result == ["Show my alerts", "Compare TCS vs INFY", "Top gainers today", "Set a price alert"]

    def test_generate_suggestions_falls_back_on_llm_failure(self):
        # LLM chips are a BYO-user feature since LLM-11 (trial chips are deterministic).
        orch = _build_orchestrator(MagicMock())
        orch._provider_config = {"api_key": "k", "model": "m"}
        fake_client = MagicMock()
        fake_client.ask.side_effect = RuntimeError("rate limited")
        with patch("agents.llm_client.LLMClient", return_value=fake_client):
            result = orch.generate_suggestions("resp", "query")
        assert result == ["Analyse a stock", "Compare mutual funds", "Top gainers today", "Set a price alert"]

    def test_trial_suggestions_need_no_llm_call(self):
        orch = _build_orchestrator(MagicMock())
        with patch("agents.llm_client.LLMClient") as client_cls:
            chips = orch.generate_suggestions("resp", "analyse TCS fundamentals")
        client_cls.assert_not_called()
        assert len(chips) == 4 and any("TCS" in c for c in chips)

    def test_reset_clears_awaiting_confirmation(self):
        orch = _build_orchestrator(MagicMock())
        orch._awaiting_confirmation = True
        orch.reset()
        assert orch._awaiting_confirmation is False


class TestTurnDeadline:
    """REL-02: every turn carries a deadline, a resume after a gate gets a
    fresh one, and research that didn't make it is named in the answer."""

    def test_fresh_turn_is_stamped_with_a_deadline(self):
        import time

        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {"final_report": "x"}
        orch = _build_orchestrator(fake_graph)
        list(orch.process_stream("analyse TCS"))
        state = fake_graph.invoke.call_args[0][0]
        assert state["research_cutoff_ts"] < state["deadline_ts"]
        assert state["deadline_ts"] > time.time()

    def test_resume_gets_a_fresh_deadline(self):
        fake_graph = MagicMock()
        fake_interrupt = MagicMock()
        fake_interrupt.value = {"type": "research_confirmation", "message": "Proceed?"}
        fake_graph.invoke.side_effect = [{"__interrupt__": [fake_interrupt]}, {"final_report": "done"}]
        orch = _build_orchestrator(fake_graph)
        list(orch.process_stream("compare six stocks"))
        list(orch.process_stream("yes"))
        resume = fake_graph.invoke.call_args_list[1][0][0]
        assert set(resume.update) == {"deadline_ts", "research_cutoff_ts"}

    def test_missing_data_is_named_in_the_answer(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {
            "final_report": "TCS trades at 4000.",
            "tasks": [
                {"tool": "fetch_info", "entity": "TCS", "status": "done"},
                {"tool": "fetch_news", "entity": "TCS", "status": "failed", "error": "timeout"},
            ],
        }
        orch = _build_orchestrator(fake_graph)
        text = list(orch.process_stream("TCS price and news"))[-1][0]
        assert text.startswith("TCS trades at 4000.")
        assert "news for TCS (timed out)" in text


class TestTurnRecords:
    """OBS-02: every process_stream call writes exactly one turns row, with a
    status that says how the turn ended."""

    def _statuses(self, fake_graph, messages, quota_exceeded=False):
        orch = _build_orchestrator(fake_graph)
        if quota_exceeded:
            orch._trial_quota_exceeded = lambda: True
        with patch("utils.db.turn_record") as row:
            for m in messages:
                with contextlib.suppress(RuntimeError):
                    list(orch.process_stream(m))
        return [c.kwargs["status"] for c in row.call_args_list]

    def test_ok_and_partial(self):
        g = MagicMock()
        g.invoke.side_effect = [
            {"final_report": "fine", "intent": "equity", "tasks": [{"tool": "fetch_info", "status": "done"}]},
            {"final_report": "half", "tasks": [{"tool": "fetch_news", "entity": "TCS", "status": "failed"}]},
        ]
        assert self._statuses(g, ["a", "b"]) == ["ok", "partial"]

    def test_gated_then_resumed(self):
        g = MagicMock()
        interrupt = MagicMock()
        interrupt.value = {"message": "Proceed?"}
        g.invoke.side_effect = [{"__interrupt__": [interrupt]}, {"final_report": "done"}]
        assert self._statuses(g, ["compare", "yes"]) == ["gated", "ok"]

    def test_quota_and_unexpected_error(self):
        assert self._statuses(MagicMock(), ["hi"], quota_exceeded=True) == ["quota"]
        g = MagicMock()
        g.invoke.side_effect = RuntimeError("graph blew up")
        assert self._statuses(g, ["hi"]) == ["error"]


class TestStreamCutOff:
    """REL-06: a synthesis stream that dies part-way keeps what arrived."""

    def _orch(self):
        fake_graph = MagicMock()
        fake_graph.invoke.return_value = {
            "final_report": "",
            "pending_synthesis_prompt": "PROMPT",
            "tasks": [],
        }
        return _build_orchestrator(fake_graph)

    def test_partial_answer_kept_with_note_and_no_exception_text(self):
        from graph.deadline import CUT_OFF_NOTE

        def broken_stream(*_a, **_k):
            yield "TCS trades "
            yield "at a P/E of 30."
            raise ConnectionError("upstream reset at 10.0.0.1:443")

        orch = self._orch()
        with patch("agents.llm_client.LLMClient") as client:
            client.return_value.stream.side_effect = broken_stream
            results = list(orch.process_stream("analyse TCS"))
        final = results[-1][0]
        assert final.startswith("TCS trades at a P/E of 30.")
        assert CUT_OFF_NOTE.strip() in final
        assert "10.0.0.1" not in final and "ConnectionError" not in final

    def test_failure_before_any_text_reaches_the_error_boundary(self):
        def dead_stream(*_a, **_k):
            raise ConnectionError("boom")
            yield  # pragma: no cover

        orch = self._orch()
        with patch("agents.llm_client.LLMClient") as client:
            client.return_value.stream.side_effect = dead_stream
            gen = orch.process_stream("analyse TCS")
            with pytest.raises(ConnectionError):
                list(gen)
