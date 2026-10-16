"""OBS-03: turns, LLM calls and feedback feed Prometheus metrics; /metrics is
token-protected outside dev/test; the dashboard covers every PRD panel."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils import metrics, tracing


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_a_finished_turn_is_counted_and_timed():
    before = _sample("aria_turns_total", {"intent": "equity", "status": "ok"})
    hits = _sample("aria_tool_calls_total", {"cache": "hit"})
    with (
        patch.object(tracing, "_persist"),
        tracing.turn("00000000-0000-4000-8000-0000000000aa", 1, "s", "q") as rec,
    ):
        rec.intent, rec.status = "equity", "ok"
        rec.first_output()
        rec.add_tool_call(cache_hit=True)
        rec.add_tool_call(cache_hit=False)
        rec.add_parse_failure("planner")
        rec.grounding = {"unsupported": ["99"]}
    assert _sample("aria_turns_total", {"intent": "equity", "status": "ok"}) == before + 1
    assert _sample("aria_turn_ttft_seconds_count", {"intent": "equity"}) >= 1
    assert _sample("aria_tool_calls_total", {"cache": "hit"}) == hits + 1
    assert _sample("aria_parse_failures_total", {"node": "planner"}) >= 1
    assert _sample("aria_grounding_checks_total", {"result": "ungrounded"}) >= 1


def test_llm_calls_tokens_cost_and_fallbacks():
    calls = _sample(
        "aria_llm_calls_total", {"node": "planner", "provider": "gemini", "model": "gemini-2.5-flash"}
    )
    metrics.observe_llm_call("planner", "gemini", "gemini-2.5-flash", 100, 20, 0.05, fallback=True)
    assert (
        _sample(
            "aria_llm_calls_total", {"node": "planner", "provider": "gemini", "model": "gemini-2.5-flash"}
        )
        == calls + 1
    )
    assert _sample("aria_llm_fallbacks_total", {"node": "planner"}) >= 1
    assert _sample("aria_llm_cost_inr_total") >= 0.05


def test_live_gauges_read_at_scrape_time():
    beats = [{"job": "alert_check", "last_ok_at": None, "last_status": "error"}]
    with (
        patch(
            "utils.db.pool_stats",
            return_value={"pool_size": 5, "pool_available": 2, "requests_waiting": 0, "pool_max": 20},
        ),
        patch("utils.db.job_heartbeats", return_value=beats),
        patch("utils.resilience.breaker_states", return_value={"nse": {"state": "open"}}),
    ):
        text = metrics.render().decode()
    assert 'aria_db_pool_connections{state="in_use"} 3.0' in text
    assert 'aria_job_failing{job="alert_check"} 1.0' in text
    assert 'aria_data_source_up{source="nse"} 0.0' in text


def _client():
    from web.ops_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_metrics_route_needs_a_token_outside_dev():
    with patch("config.METRICS_TOKEN", ""), patch("config.APP_ENV", "dev"):
        assert _client().get("/metrics").status_code == 200
    with patch("config.METRICS_TOKEN", ""), patch("config.APP_ENV", "prod"):
        assert _client().get("/metrics").status_code == 404
    with patch("config.METRICS_TOKEN", "s3cret-token"), patch("config.APP_ENV", "prod"):
        c = _client()
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code == 200 and "aria_turns_total" in r.text


def test_dashboard_has_every_prd_panel():
    dash = json.loads(Path("ops/grafana/dashboards/aria.json").read_text(encoding="utf-8"))
    exprs = " ".join(t["expr"] for p in dash["panels"] for t in p["targets"])
    for metric in (
        "aria_turns_total",
        "aria_turn_ttft_seconds_bucket",
        "aria_turn_latency_seconds_bucket",
        "aria_llm_calls_total",
        "aria_llm_tokens_total",
        "aria_llm_cost_inr_total",
        "aria_llm_fallbacks_total",
        "aria_tool_calls_total",
        "aria_parse_failures_total",
        "aria_grounding_checks_total",
        "aria_feedback_total",
        "aria_job_last_ok_age_seconds",
        "aria_db_pool_connections",
    ):
        assert metric in exprs, metric
    exposed = metrics.render().decode()
    for name in ("aria_turns_total", "aria_llm_calls_total", "aria_feedback_total"):
        assert f"# TYPE {name} counter" in exposed
