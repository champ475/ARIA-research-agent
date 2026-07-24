"""
scripts/measure_graph_latency.py
Phase 7.0 baseline measurement (see RFC_AGENTIC_RESEARCH_GRAPH.md §17/§19 and
the graph-expansion plan). Runs a fixed set of representative equity queries
through the real LangGraph pipeline (graph/graph.py::build_graph) and reports
LLM-call count + wall-clock time per turn, so later phases (7.1-7.3, which add
5 more domains to the tool catalogue) have a pre-expansion control to compare
against.

Manual-run only — NOT wired into CI or pytest, since it makes real Groq/Gemini
calls and would burn daily quota (14,400 req/day/key) on every test run.
Requires GROQ_API_KEY_1 (or GEMINI_API_KEY) configured in .env, same as the app.

Usage:
    python scripts/measure_graph_latency.py
"""
from __future__ import annotations

import statistics
import sys
import time
from unittest.mock import patch

from agents.llm_client import LLMClient
from graph.graph import build_graph

# One "simple" (no research needed) and one "complex" (multi-section analysis)
# query, matching QueryAnalyzer's own complexity classification so the two
# rows in RFC §19's table stay directly comparable across re-measurements.
_QUERIES = [
    ("simple", "what is the current price of TCS"),
    ("complex", "give me a full analysis of Reliance Industries — trend, valuation, risk, and a buy/sell/hold call"),
]


class _CallCounter:
    """Wraps LLMClient.ask/chat/stream with call-counting, patched in for the
    duration of one measurement run. Uses unittest.mock.patch.object(...,
    wraps=original) so the real provider call still happens — this measures
    real LLM traffic, it doesn't stub it out."""

    def __init__(self):
        self.count = 0

    def make_wrapper(self, original):
        def wrapper(client_self, *args, **kwargs):
            self.count += 1
            return original(client_self, *args, **kwargs)
        return wrapper

    def make_stream_wrapper(self, original):
        def wrapper(client_self, *args, **kwargs):
            self.count += 1
            yield from original(client_self, *args, **kwargs)
        return wrapper


def _run_one(query: str) -> tuple[int, float]:
    counter = _CallCounter()
    with patch.object(LLMClient, "ask", counter.make_wrapper(LLMClient.ask)), \
         patch.object(LLMClient, "chat", counter.make_wrapper(LLMClient.chat)), \
         patch.object(LLMClient, "stream", counter.make_stream_wrapper(LLMClient.stream)):
        graph = build_graph(checkpointer=None)
        start = time.monotonic()
        graph.invoke({"query": query, "max_loops": 2}, config={"configurable": {"thread_id": "measure-graph-latency"}})
        elapsed = time.monotonic() - start
    return counter.count, elapsed


def main():
    print(f"{'complexity':<10} {'query':<70} {'llm_calls':<10} {'wall_clock_s':<12}")
    by_complexity: dict[str, list[tuple[int, float]]] = {}
    for complexity, query in _QUERIES:
        try:
            calls, elapsed = _run_one(query)
        except Exception as e:
            print(f"{complexity:<10} {query[:68]:<70} FAILED: {e}", file=sys.stderr)
            continue
        by_complexity.setdefault(complexity, []).append((calls, elapsed))
        print(f"{complexity:<10} {query[:68]:<70} {calls:<10} {elapsed:<12.2f}")

    print("\n--- Averages (fill into RFC_AGENTIC_RESEARCH_GRAPH.md §19) ---")
    for complexity, rows in by_complexity.items():
        avg_calls = statistics.mean(c for c, _ in rows)
        avg_time = statistics.mean(t for _, t in rows)
        print(f"{complexity}: avg {avg_calls:.1f} LLM calls/turn, avg {avg_time:.2f}s wall-clock/turn")


if __name__ == "__main__":
    main()
