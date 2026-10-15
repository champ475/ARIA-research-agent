"""
utils/tracing.py — per-turn traces (OBS-02). The only module that knows about
LangSmith.

Every chat turn gets a TurnRecord (in a contextvar, so LangGraph's worker
threads and utils/resilience pools see it) that accumulates what the turn
did: LLM calls, tokens, providers, prompt versions, tool calls, cache hits,
node timings, data gaps and the grounding result (LLM-03). At the end it
becomes one row in the `turns` table — always, whether or not LangSmith is
on — so an error ID shown to a user (its turn_id prefix) leads to the turn,
and from there to the trace.

LangSmith (optional): set ARIA_TRACING=langsmith and LANGSMITH_API_KEY
(LANGSMITH_PROJECT, LANGSMITH_ENDPOINT for the EU region). Spans are posted
as explicit RunTrees with parents we track ourselves. We deliberately never
make a run "current" in LangSmith's own context: that would switch on
LangChain's automatic tracing, which uploads LangGraph's full state
(history, profiles) unredacted. Everything we send passes through
`redact_for_trace` first.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from utils.logging_setup import redact

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MAX_TRACE_TEXT = 4000


def redact_for_trace(value: Any, depth: int = 0) -> Any:
    """Secrets and emails masked, long text cut, nesting bounded."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        text = _EMAIL.sub("[email]", redact(value))
        return text if len(text) <= _MAX_TRACE_TEXT else text[:_MAX_TRACE_TEXT] + "…[truncated]"
    if isinstance(value, dict):
        return {str(k): redact_for_trace(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [redact_for_trace(v, depth + 1) for v in list(value)[:50]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_for_trace(str(value), depth + 1)


def prompt_version(system_prompt: str) -> str:
    """Content hash of a system prompt — changes whenever the prompt does,
    with no manual version bumps to forget."""
    return hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()[:8]


# ── LangSmith sink ────────────────────────────────────────────────────────────


def tracing_enabled() -> bool:
    return os.getenv("ARIA_TRACING", "").strip().lower() == "langsmith" and bool(
        os.getenv("LANGSMITH_API_KEY")
    )


def warn_if_auto_tracing_on() -> None:
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2"):
        if os.getenv(var, "").strip().lower() == "true":
            log.warning(
                f"[tracing] {var}=true switches on LangChain auto-tracing, which uploads full, "
                "unredacted graph state (chat history, financial profiles). Unset it and use "
                "ARIA_TRACING=langsmith instead."
            )


class _LangSmithSink:
    """Thin adapter over langsmith.RunTree; replaced by a stub in tests."""

    def __init__(self) -> None:
        from langsmith import Client

        self._client = Client()
        self._project = os.getenv("LANGSMITH_PROJECT", "aria")

    def start(self, parent: Any, name: str, run_type: str, inputs: dict, metadata: dict) -> Any:
        from langsmith.run_trees import RunTree

        if parent is None:
            run = RunTree(
                name=name,
                run_type=run_type,
                inputs=inputs,
                extra={"metadata": metadata},
                ls_client=self._client,
                project_name=self._project,
            )
        else:
            run = parent.create_child(
                name=name, run_type=run_type, inputs=inputs, extra={"metadata": metadata}
            )
        run.post()
        return run

    def end(self, run: Any, outputs: dict | None, error: str | None) -> None:
        run.end(outputs=outputs, error=error)
        run.patch()

    def url(self, run: Any) -> str | None:
        return self._client.get_run_url(run=run, project_name=self._project)


_sink: Any = None
_sink_lock = threading.Lock()


def _get_sink() -> Any:
    global _sink
    if not tracing_enabled():
        return None
    with _sink_lock:
        if _sink is None:
            try:
                _sink = _LangSmithSink()
            except Exception as e:  # tracing must never break a turn
                log.warning(f"[tracing] LangSmith unavailable, tracing off: {e}")
                return None
        return _sink


def set_sink(sink: Any) -> None:
    """Tests: install a stub sink (or None)."""
    global _sink
    with _sink_lock:
        _sink = sink


# ── the per-turn record ───────────────────────────────────────────────────────


@dataclass
class TurnRecord:
    turn_id: str
    user_id: int | None
    session_id: str | None
    started: float = field(default_factory=time.monotonic)
    ttft_ms: int | None = None
    intent: str | None = None
    complexity: str | None = None
    status: str = "error"  # overwritten on a clean finish; an exception leaves "error"
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = False
    providers: set[str] = field(default_factory=set)
    prompt_versions: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    cache_hits: int = 0
    node_ms: dict[str, int] = field(default_factory=dict)
    data_gaps: int = 0
    grounding: dict | None = None
    trace_url: str | None = None
    root_run: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def error_id(self) -> str:
        return self.turn_id.replace("-", "")[:8]

    def first_output(self) -> None:
        with self._lock:
            if self.ttft_ms is None:
                self.ttft_ms = int((time.monotonic() - self.started) * 1000)

    def add_llm_call(
        self, provider: str, version: str, tokens_in: int, tokens_out: int, estimated: bool
    ) -> None:
        with self._lock:
            self.llm_calls += 1
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            self.tokens_estimated = self.tokens_estimated or estimated
            self.providers.add(provider)
            self.prompt_versions[version] = self.prompt_versions.get(version, 0) + 1

    def add_tool_call(self, cache_hit: bool) -> None:
        with self._lock:
            self.tool_calls += 1
            self.cache_hits += int(cache_hit)

    def add_node_time(self, node: str, ms: int) -> None:
        with self._lock:
            self.node_ms[node] = self.node_ms.get(node, 0) + ms


_current: contextvars.ContextVar[TurnRecord | None] = contextvars.ContextVar("aria_turn", default=None)
_current_span: contextvars.ContextVar[Any] = contextvars.ContextVar("aria_span", default=None)


def current() -> TurnRecord | None:
    return _current.get()


def usage_from_response(r: Any) -> tuple[int, int] | None:
    """(prompt_tokens, completion_tokens) from an OpenAI/Groq-style response
    (`.usage`) or a google-genai one (`.usage_metadata`), else None."""
    u = getattr(r, "usage", None)
    if u is not None and getattr(u, "prompt_tokens", None) is not None:
        return int(u.prompt_tokens or 0), int(getattr(u, "completion_tokens", 0) or 0)
    m = getattr(r, "usage_metadata", None)
    if m is not None and getattr(m, "prompt_token_count", None) is not None:
        return int(m.prompt_token_count or 0), int(getattr(m, "candidates_token_count", 0) or 0)
    return None


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


@contextmanager
def span(
    name: str, run_type: str = "chain", inputs: dict | None = None, metadata: dict | None = None
) -> Iterator[dict]:
    """A child span of whatever span is current. Yields a dict the caller can
    fill with outputs. A no-op (still yields) when tracing is off."""
    outputs: dict = {}
    sink = _get_sink()
    parent = _current_span.get()
    rec = current()
    if sink is None or (parent is None and (rec is None or rec.root_run is None)):
        yield outputs
        return
    try:
        run = sink.start(
            parent or rec.root_run,  # type: ignore[union-attr]
            name,
            run_type,
            redact_for_trace(inputs or {}),
            metadata or {},
        )
    except Exception as e:
        log.debug(f"[tracing] span start failed: {e}")
        yield outputs
        return
    token = _current_span.set(run)
    error: str | None = None
    try:
        yield outputs
    except BaseException as e:
        error = f"{type(e).__name__}: {redact(str(e))[:500]}"
        raise
    finally:
        _current_span.reset(token)
        try:
            sink.end(run, redact_for_trace(outputs), error)
        except Exception as e:
            log.debug(f"[tracing] span end failed: {e}")


@contextmanager
def turn(turn_id: str, user_id: int | None, session_id: str | None, query: str) -> Iterator[TurnRecord]:
    """Opens the turn's record (and root span); on exit writes its `turns`
    row. Persistence and tracing failures are logged, never raised."""
    rec = TurnRecord(turn_id=turn_id, user_id=user_id, session_id=session_id)
    sink = _get_sink()
    if sink is not None:
        try:
            rec.root_run = sink.start(
                None,
                "chat_turn",
                "chain",
                redact_for_trace({"query": query}),
                {"turn_id": turn_id, "error_id": rec.error_id},
            )
        except Exception as e:
            log.warning(f"[tracing] could not start trace: {e}")
    token = _current.set(rec)
    span_token = _current_span.set(rec.root_run)
    try:
        yield rec
    finally:
        _current_span.reset(span_token)
        _current.reset(token)
        latency_ms = int((time.monotonic() - rec.started) * 1000)
        if sink is not None and rec.root_run is not None:
            try:
                sink.end(
                    rec.root_run,
                    {"status": rec.status, "intent": rec.intent, "latency_ms": latency_ms},
                    None if rec.status != "error" else "turn failed",
                )
            except Exception as e:
                log.debug(f"[tracing] could not end trace: {e}")
        _persist(rec, latency_ms, sink)


def _persist(rec: TurnRecord, latency_ms: int, sink: Any) -> None:
    from utils.db import turn_record, turn_set_trace_url

    try:
        turn_record(
            turn_id=rec.turn_id,
            user_id=rec.user_id,
            session_id=rec.session_id,
            intent=rec.intent,
            complexity=rec.complexity,
            status=rec.status,
            error_id=rec.error_id,
            latency_ms=latency_ms,
            ttft_ms=rec.ttft_ms,
            llm_calls=rec.llm_calls,
            tokens_in=rec.tokens_in,
            tokens_out=rec.tokens_out,
            tokens_estimated=rec.tokens_estimated,
            providers=sorted(rec.providers),
            prompt_versions=rec.prompt_versions,
            tool_calls=rec.tool_calls,
            cache_hits=rec.cache_hits,
            node_ms=rec.node_ms,
            data_gaps=rec.data_gaps,
            grounding=rec.grounding,
        )
    except Exception as e:
        log.warning(f"[tracing] could not record turn {rec.error_id}: {e}")
        return
    if sink is None or rec.root_run is None:
        return

    def _attach_url() -> None:
        # Resolving the URL is a network call; do it off the request path.
        try:
            url = sink.url(rec.root_run)
            if url:
                turn_set_trace_url(rec.turn_id, url)
        except Exception as e:
            log.debug(f"[tracing] could not attach trace URL: {e}")

    threading.Thread(target=_attach_url, name="aria-trace-url", daemon=True).start()


def _state_summary(state: Any) -> dict:
    """What a node span records as input: counts and routing facts, never
    the raw state (history, evidence payloads, profiles)."""
    if not isinstance(state, dict):
        return {}
    if "task" in state:  # an execute_task_node Send payload
        task = state.get("task") or {}
        return {"tool": task.get("tool"), "entity": task.get("entity")}
    return {
        "intent": state.get("intent"),
        "complexity": state.get("complexity"),
        "tasks": len(state.get("tasks") or []),
        "evidence": len(state.get("evidence") or []),
        "loop_count": state.get("loop_count"),
    }


def traced_node(name: str, fn: Any) -> Any:
    """Wrap a LangGraph node: a span per run (when tracing is on) and the
    node's wall time added to the turn record (always)."""

    def wrapper(state: Any) -> Any:
        started = time.monotonic()
        try:
            with span(name, "chain", inputs=_state_summary(state)) as out:
                update = fn(state)
                if isinstance(update, dict):
                    journal = update.get("research_journal") or []
                    out["updated"] = sorted(k for k in update if k != "research_journal")
                    if journal:
                        out["decision"] = journal[-1].get("decision")
                return update
        finally:
            rec = current()
            if rec is not None:
                rec.add_node_time(name, int((time.monotonic() - started) * 1000))

    wrapper.__name__ = getattr(fn, "__name__", name)
    return wrapper
