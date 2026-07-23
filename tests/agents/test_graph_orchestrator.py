from unittest.mock import MagicMock, patch

from agents.graph_orchestrator import GraphOrchestrator


def _build_orchestrator(fake_graph):
    with patch("agents.graph_orchestrator.build_graph", return_value=fake_graph), \
         patch("agents.graph_orchestrator.get_postgres_checkpointer", return_value=None):
        return GraphOrchestrator(user_id=1)


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

        orch.process_stream("analyse RELIANCE")
        list(orch.process_stream("analyse RELIANCE"))  # generator, must iterate

        args, kwargs = fake_graph.invoke.call_args
        assert args[0]["query"] == "analyse RELIANCE"
        assert kwargs["config"]["configurable"]["thread_id"] == "user-1"


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


class TestGraphOrchestratorMisc:
    def test_generate_suggestions_returns_empty_list(self):
        orch = _build_orchestrator(MagicMock())
        assert orch.generate_suggestions("resp", "query") == []

    def test_reset_clears_awaiting_confirmation(self):
        orch = _build_orchestrator(MagicMock())
        orch._awaiting_confirmation = True
        orch.reset()
        assert orch._awaiting_confirmation is False
