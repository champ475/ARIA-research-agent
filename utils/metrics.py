"""
utils/metrics.py — Prometheus metrics for dashboards and alerts (OBS-03).

Served at GET /metrics (web/ops_routes.py). Counters and histograms are fed
at the source — one call per finished turn (utils/tracing.turn), per LLM
call (agents/llm_client.py), per feedback rating — and the gauges (DB pool,
job heartbeat age, data-source breakers) are read when Prometheus scrapes.
Labels are low-cardinality on purpose: intent, status, node, provider,
model, source, job — never a user, symbol or turn id.

The `turns` table stays the source of truth for history (SQL views in
migration 0019, scripts/slo_report.py); these are the live, per-process
numbers Grafana and the alert rules (ops/prometheus/alerts.yml) read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()  # no auto_describe: the live gauges must not hit the DB at import

_LATENCY_BUCKETS = (0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 300)

TURNS = Counter("aria_turns", "Finished chat turns", ["intent", "status"], registry=REGISTRY)
TURN_LATENCY = Histogram(
    "aria_turn_latency_seconds", "Whole-turn latency", ["intent"], buckets=_LATENCY_BUCKETS, registry=REGISTRY
)
TURN_TTFT = Histogram(
    "aria_turn_ttft_seconds", "Time to first output", ["intent"], buckets=_LATENCY_BUCKETS, registry=REGISTRY
)
LLM_CALLS = Counter("aria_llm_calls", "LLM calls", ["node", "provider", "model"], registry=REGISTRY)
LLM_TOKENS = Counter("aria_llm_tokens", "LLM tokens", ["direction"], registry=REGISTRY)
LLM_COST = Counter(
    "aria_llm_cost_inr", "Estimated LLM cost on the operator's keys, rupees", registry=REGISTRY
)
LLM_FALLBACKS = Counter(
    "aria_llm_fallbacks",
    "LLM calls served by a provider other than the first choice",
    ["node"],
    registry=REGISTRY,
)
TOOL_CALLS = Counter("aria_tool_calls", "Research tool calls", ["cache"], registry=REGISTRY)
PARSE_FAILURES = Counter(
    "aria_parse_failures", "Unusable structured LLM replies", ["node"], registry=REGISTRY
)
GROUNDING = Counter(
    "aria_grounding_checks", "Answers checked for unsupported numbers", ["result"], registry=REGISTRY
)
FEEDBACK = Counter("aria_feedback", "Answer ratings", ["rating"], registry=REGISTRY)
LLM_EXHAUSTED = Counter("aria_llm_exhausted", "Turns where every LLM provider failed", registry=REGISTRY)


def _intent(v: str | None) -> str:
    return (v or "unknown")[:20]


def observe_turn(rec: Any, latency_ms: int) -> None:
    """Called once per finished turn with its TurnRecord (utils/tracing)."""
    try:
        intent = _intent(rec.intent)
        TURNS.labels(intent, rec.status or "error").inc()
        TURN_LATENCY.labels(intent).observe(latency_ms / 1000)
        if rec.ttft_ms is not None:
            TURN_TTFT.labels(intent).observe(rec.ttft_ms / 1000)
        if rec.tool_calls:
            TOOL_CALLS.labels("hit").inc(rec.cache_hits)
            TOOL_CALLS.labels("miss").inc(rec.tool_calls - rec.cache_hits)
        for node, n in (rec.parse_failures or {}).items():
            PARSE_FAILURES.labels(node).inc(n)
        if rec.grounding is not None:
            GROUNDING.labels("ok" if not rec.grounding.get("unsupported") else "ungrounded").inc()
        if rec.status == "llm_unavailable":
            LLM_EXHAUSTED.inc()
    except Exception:
        log.debug("[metrics] could not record turn", exc_info=True)


def observe_llm_call(
    node: str | None,
    provider: str,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_inr: float,
    fallback: bool,
) -> None:
    try:
        LLM_CALLS.labels(node or "other", provider, (model or "unknown")[:40]).inc()
        LLM_TOKENS.labels("in").inc(tokens_in)
        LLM_TOKENS.labels("out").inc(tokens_out)
        if cost_inr:
            LLM_COST.inc(cost_inr)
        if fallback:
            LLM_FALLBACKS.labels(node or "other").inc()
    except Exception:
        log.debug("[metrics] could not record LLM call", exc_info=True)


def observe_feedback(rating: int) -> None:
    FEEDBACK.labels("up" if rating > 0 else "down").inc()


class _LiveGauges(Collector):
    """Read at scrape time: DB pool, job heartbeats, data-source breakers."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            from utils.db import pool_stats

            st = pool_stats()
            pool = GaugeMetricFamily("aria_db_pool_connections", "App DB pool", labels=["state"])
            pool.add_metric(["in_use"], st["pool_size"] - st["pool_available"])
            pool.add_metric(["available"], st["pool_available"])
            pool.add_metric(["max"], st["pool_max"])
            pool.add_metric(["waiting"], st["requests_waiting"])
            yield pool
        except Exception:
            log.debug("[metrics] pool stats unavailable", exc_info=True)
        try:
            from utils.db import job_heartbeats

            age = GaugeMetricFamily(
                "aria_job_last_ok_age_seconds",
                "Seconds since each background job last succeeded",
                labels=["job"],
            )
            failing = GaugeMetricFamily("aria_job_failing", "1 if a job's last run failed", labels=["job"])
            now = time.time()
            for row in job_heartbeats():
                ok = row.get("last_ok_at")
                age.add_metric([row["job"]], now - ok.timestamp() if ok else float("inf"))
                failing.add_metric([row["job"]], 1.0 if row.get("last_status") == "error" else 0.0)
            yield age
            yield failing
        except Exception:
            log.debug("[metrics] job heartbeats unavailable", exc_info=True)
        try:
            from utils.resilience import breaker_states

            up = GaugeMetricFamily(
                "aria_data_source_up", "1 while a source's breaker is closed", labels=["source"]
            )
            for name, s in breaker_states().items():
                up.add_metric([name], 1.0 if s["state"] == "closed" else 0.0)
            yield up
        except Exception:
            log.debug("[metrics] breaker states unavailable", exc_info=True)


REGISTRY.register(_LiveGauges())


def render() -> bytes:
    return generate_latest(REGISTRY)
