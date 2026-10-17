"""Generates ops/grafana/dashboards/aria.json (OBS-03): edit the panel list
here and run `python ops/grafana/build_dashboard.py`."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DS = {"type": "prometheus", "uid": "aria-prometheus"}
FAILED = 'status=~"error|llm_unavailable"'

PANELS = [
    (
        "Turns by intent (per min)",
        "timeseries",
        ["sum by (intent) (rate(aria_turns_total[5m])) * 60"],
        "short",
    ),
    (
        "Error rate (SLO 99.5%)",
        "timeseries",
        [f"sum(rate(aria_turns_total{{{FAILED}}}[10m])) / clamp_min(sum(rate(aria_turns_total[10m])), 1e-9)"],
        "percentunit",
    ),
    (
        "Time to first output p50 / p95 (equity SLO ≤ 15 s)",
        "timeseries",
        [
            "histogram_quantile(0.5, sum by (le) (rate(aria_turn_ttft_seconds_bucket[5m])))",
            "histogram_quantile(0.95, sum by (le) (rate(aria_turn_ttft_seconds_bucket[5m])))",
            'histogram_quantile(0.95, sum by (le) (rate(aria_turn_ttft_seconds_bucket{intent="equity"}[5m])))',
        ],
        "s",
    ),
    (
        "Turn latency p50 / p95",
        "timeseries",
        [
            "histogram_quantile(0.5, sum by (le) (rate(aria_turn_latency_seconds_bucket[5m])))",
            "histogram_quantile(0.95, sum by (le) (rate(aria_turn_latency_seconds_bucket[5m])))",
        ],
        "s",
    ),
    (
        "LLM calls per turn",
        "timeseries",
        ["sum(rate(aria_llm_calls_total[15m])) / clamp_min(sum(rate(aria_turns_total[15m])), 1e-9)"],
        "short",
    ),
    (
        "LLM tokens (per min)",
        "timeseries",
        ["sum by (direction) (rate(aria_llm_tokens_total[5m])) * 60"],
        "short",
    ),
    ("LLM cost (₹ per hour)", "timeseries", ["sum(increase(aria_llm_cost_inr_total[1h]))"], "currencyINR"),
    (
        "LLM cost per turn (₹)",
        "stat",
        ["sum(increase(aria_llm_cost_inr_total[24h])) / clamp_min(sum(increase(aria_turns_total[24h])), 1)"],
        "currencyINR",
    ),
    (
        "Provider fallback rate",
        "timeseries",
        ["sum(rate(aria_llm_fallbacks_total[15m])) / clamp_min(sum(rate(aria_llm_calls_total[15m])), 1e-9)"],
        "percentunit",
    ),
    (
        "Evidence cache hit rate",
        "timeseries",
        [
            'sum(rate(aria_tool_calls_total{cache="hit"}[15m])) / clamp_min(sum(rate(aria_tool_calls_total[15m])), 1e-9)'
        ],
        "percentunit",
    ),
    (
        "Parse failures by node (per hour)",
        "timeseries",
        ["sum by (node) (increase(aria_parse_failures_total[1h]))"],
        "short",
    ),
    (
        "Ungrounded-number rate",
        "timeseries",
        [
            'sum(rate(aria_grounding_checks_total{result="ungrounded"}[1h])) / '
            "clamp_min(sum(rate(aria_grounding_checks_total[1h])), 1e-9)"
        ],
        "percentunit",
    ),
    ("Feedback (per day)", "timeseries", ["sum by (rating) (increase(aria_feedback_total[1d]))"], "short"),
    ("Background job: time since last success", "bargauge", ["aria_job_last_ok_age_seconds"], "s"),
    ("DB pool connections", "timeseries", ["aria_db_pool_connections"], "short"),
    ("Data sources up", "stat", ["aria_data_source_up"], "short"),
]


def panel(i, title, kind, exprs, unit):
    return {
        "id": i + 1,
        "title": title,
        "type": kind,
        "datasource": DS,
        "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(65 + j), "datasource": DS, "expr": e, "legendFormat": "__auto"}
            for j, e in enumerate(exprs)
        ],
    }


dash = {
    "uid": "aria-overview",
    "title": "ARIA — production overview",
    "tags": ["aria"],
    "timezone": "Asia/Kolkata",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "panels": [panel(i, *p) for i, p in enumerate(PANELS)],
}
out = ROOT / "ops" / "grafana" / "dashboards" / "aria.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
sys.stdout.write(f"{len(PANELS)} panels -> {out}\n")
