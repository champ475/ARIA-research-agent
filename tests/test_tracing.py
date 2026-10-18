"""OBS-02: one turns row per turn whatever happens, redaction of everything
sent to LangSmith, no LangChain auto-tracing, and per-call accounting."""

from unittest.mock import MagicMock, patch

import pytest

from utils import tracing


@pytest.fixture(autouse=True)
def _tracing_off(monkeypatch):
    monkeypatch.delenv("ARIA_TRACING", raising=False)
    tracing.set_sink(None)
    yield
    tracing.set_sink(None)


class _StubSink:
    def __init__(self):
        self.started: list[dict] = []
        self.ended: list[dict] = []

    def start(self, parent, name, run_type, inputs, metadata):
        run = {"name": name, "parent": parent["name"] if parent else None, "inputs": inputs}
        self.started.append(run)
        return run

    def end(self, run, outputs, error):
        self.ended.append({"name": run["name"], "outputs": outputs, "error": error})

    def url(self, run):
        return "https://smith.example/r/1"


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setenv("ARIA_TRACING", "langsmith")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    s = _StubSink()
    tracing.set_sink(s)
    return s


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_redaction_masks_secrets_emails_and_cuts_long_text():
    out = tracing.redact_for_trace(
        {"q": "mail me at a.b@example.com with key gsk_abcdefghijklmnopqrstuvwxyz0123", "big": "x" * 9000}
    )
    assert "a.b@example.com" not in out["q"] and "[email]" in out["q"]
    assert "gsk_abcdefghijklmnopqrstuvwxyz0123" not in out["q"]
    assert out["big"].endswith("…[truncated]") and len(out["big"]) < 4100


def test_prompt_version_tracks_prompt_content():
    assert tracing.prompt_version("a") == tracing.prompt_version("a")
    assert tracing.prompt_version("a") != tracing.prompt_version("b")
    assert len(tracing.prompt_version("a")) == 8


def test_usage_from_both_sdk_shapes():
    openai_like = MagicMock(usage=MagicMock(prompt_tokens=120, completion_tokens=30))
    gemini_like = MagicMock(spec=["usage_metadata"])
    gemini_like.usage_metadata = MagicMock(prompt_token_count=50, candidates_token_count=7)
    assert tracing.usage_from_response(openai_like) == (120, 30)
    assert tracing.usage_from_response(gemini_like) == (50, 7)
    assert tracing.usage_from_response(object()) is None


# ── one turns row per turn ────────────────────────────────────────────────────


def test_turn_writes_one_row_with_status_and_counters():
    with patch("utils.db.turn_record") as rec_row, patch("utils.db.turn_set_trace_url"):
        with tracing.turn("3f9a1c2b" + "0" * 24, 7, "s1", "price of TCS?") as rec:
            rec.add_llm_call("Groq", "abcd1234", 100, 20, estimated=False)
            rec.add_llm_call("Local", "abcd1234", 10, 5, estimated=True)
            rec.add_tool_call(cache_hit=True)
            rec.add_tool_call(cache_hit=False)
            rec.first_output()
            rec.intent, rec.status = "equity", "ok"
    kwargs = rec_row.call_args.kwargs
    assert rec_row.call_count == 1
    assert kwargs["error_id"] == "3f9a1c2b" and kwargs["status"] == "ok" and kwargs["intent"] == "equity"
    assert (kwargs["llm_calls"], kwargs["tokens_in"], kwargs["tokens_out"]) == (2, 110, 25)
    assert kwargs["tokens_estimated"] is True and kwargs["providers"] == ["Groq", "Local"]
    assert kwargs["prompt_versions"] == {"abcd1234": 2}
    assert (kwargs["tool_calls"], kwargs["cache_hits"]) == (2, 1)
    assert kwargs["ttft_ms"] is not None


def test_exception_inside_turn_still_writes_an_error_row():
    with patch("utils.db.turn_record") as rec_row, pytest.raises(RuntimeError):
        with tracing.turn("ab" * 16, 7, None, "q"):
            raise RuntimeError("boom")
    assert rec_row.call_args.kwargs["status"] == "error"


def test_db_failure_never_breaks_the_turn():
    with patch("utils.db.turn_record", side_effect=RuntimeError("db down")):
        with tracing.turn("cd" * 16, 7, None, "q") as rec:
            rec.status = "ok"  # no exception escapes on exit


# ── LangSmith ────────────────────────────────────────────────────────────────


def test_tracing_off_sends_nothing():
    assert tracing.tracing_enabled() is False
    with patch("utils.db.turn_record"), tracing.turn("ef" * 16, 1, None, "q"):
        with tracing.span("child") as out:
            out["x"] = 1  # no-op span still yields
    assert tracing._get_sink() is None


def test_spans_nest_under_the_turn_with_redacted_inputs_and_no_auto_tracing(sink):
    from langsmith.run_helpers import get_current_run_tree

    with patch("utils.db.turn_record"), patch("utils.db.turn_set_trace_url"):
        with tracing.turn("12" * 16, 1, None, "my email is x@y.com"):
            with tracing.span("Planner", inputs={"q": "key sk-abcdefghijklmnopqrstuvwxyz123456"}):
                with tracing.span("llm Groq", "llm"):
                    # LangChain auto-tracing would kick in if a run were "current".
                    assert get_current_run_tree() is None
    names = [(r["name"], r["parent"]) for r in sink.started]
    assert names == [("chat_turn", None), ("Planner", "chat_turn"), ("llm Groq", "Planner")]
    assert "x@y.com" not in str(sink.started[0]["inputs"])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in str(sink.started[1]["inputs"])
    assert [e["name"] for e in sink.ended] == ["llm Groq", "Planner", "chat_turn"]


def test_traced_node_times_node_and_summarises_state(sink):
    def node(state):
        return {"tasks": [], "research_journal": [{"decision": "planned 0 tasks"}]}

    wrapped = tracing.traced_node("Planner", node)
    with patch("utils.db.turn_record") as row, patch("utils.db.turn_set_trace_url"):
        with tracing.turn("34" * 16, 1, None, "q") as rec:
            wrapped({"query": "secret history", "history": [{"content": "x"}], "tasks": [], "evidence": []})
            rec.status = "ok"
    planner = next(r for r in sink.started if r["name"] == "Planner")
    assert "history" not in planner["inputs"] and "query" not in planner["inputs"]
    assert "Planner" in row.call_args.kwargs["node_ms"]


# ── LLMClient accounting ──────────────────────────────────────────────────────


def test_llm_client_counts_calls_with_real_or_estimated_tokens():
    from agents.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)
    primary = MagicMock()
    primary.name = "Groq"
    primary.ask.return_value = "answer"
    primary.last_usage = (90, 12)
    client._primary, client._secondary, client._local = primary, None, None
    client._is_byo, client._active, client._system = False, "Groq", "sys"
    client._prompt_version = "v1"
    with patch("utils.db.turn_record") as row, tracing.turn("56" * 16, 1, None, "q") as rec:
        client.ask("hello")
        primary.stream.return_value = iter(["a", "b"])
        list(client.stream("hello"))
        rec.status = "ok"
    kw = row.call_args.kwargs
    assert kw["llm_calls"] == 2 and kw["tokens_in"] >= 90 and kw["tokens_estimated"] is True


def test_langsmith_sink_works_against_the_installed_runtree_api(monkeypatch):
    """The real adapter, with only the network client mocked: RunTree
    construction, child creation, post/end/patch all match this langsmith
    version's API."""
    fake_client = MagicMock()
    fake_client.get_run_url.return_value = "https://smith.langchain.com/o/x/r/1"
    with patch("langsmith.Client", return_value=fake_client):
        sink = tracing._LangSmithSink()
    root = sink.start(None, "chat_turn", "chain", {"query": "q"}, {"turn_id": "t1"})
    child = sink.start(root, "Planner", "chain", {"tasks": 0}, {})
    sink.end(child, {"updated": ["tasks"]}, None)
    sink.end(root, {"status": "ok"}, None)
    assert fake_client.create_run.call_count == 2
    assert child.parent_run_id == root.id and child.trace_id == root.trace_id
    assert sink.url(root).startswith("https://")


def test_a_graph_interrupt_is_not_recorded_as_a_span_error(sink):
    class GraphInterrupt(Exception):  # the gate's pause; langgraph's class has this name
        pass

    with patch("utils.db.turn_record"), patch("utils.db.turn_set_trace_url"):
        with tracing.turn("56" * 16, 1, None, "q"):
            with pytest.raises(GraphInterrupt):
                with tracing.span("VerificationGate"):
                    raise GraphInterrupt("verification_gate")
    gate = next(e for e in sink.ended if e["name"] == "VerificationGate")
    assert gate["error"] is None
    assert gate["outputs"] == {"interrupted": True}
