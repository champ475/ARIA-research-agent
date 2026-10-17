# Service level objectives (OBS-03)

| SLO | Target | Measured from | Window |
|---|---|---|---|
| **Availability** | ≥ 99.5% of chat turns answered | `turns.status` not in (`error`, `llm_unavailable`) | calendar month |
| **Equity latency** | p95 time to first output ≤ 15 s | `turns.ttft_ms` where `intent = 'equity'` | rolling 7 days |

What doesn't count as a failure: a refused request (advice boundary), a rate or quota gate, or a cancelled turn. In each case the user got a deliberate answer, or chose to stop. A turn with missing data (`partial`) is an answer that names what was missing. It counts as good, and the `data_gaps` column tracks it separately.

## Error budget

The availability SLO allows 0.5% of turns to fail: 5 failures in 1,000 turns. `scripts/slo_report.py` prints both windows:

```
last  7 days: 1840 turns, 3 failed | availability 99.84% ✅ | error budget used 33% | equity p95 TTFT 9.4 s ✅
last 30 days: 7200 turns, 21 failed | availability 99.71% ✅ | error budget used 58% | equity p95 TTFT 10.1 s ✅
```

The budget refills at 7/30 of the monthly budget per week. If one week spends more than that, the report warns that the budget is burning too fast.

When a budget is burning:
- Pause risky changes: no new models and no prompt rewrites.
- Look at the top failing intents in `turn_stats_hourly`.
- Look at the turns' error IDs (`scripts/find_turn.py`).

## Where the numbers live

- **Live:** Prometheus scrapes `GET /metrics` (`utils/metrics.py`). The ARIA dashboard in Grafana (`ops/grafana/dashboards/aria.json`) shows turn volume by intent, error rate, p50/p95 TTFT and latency, LLM calls, tokens and ₹ cost per turn, provider fallback rate, cache hit rate, parse failures, the ungrounded-number rate, feedback, background-job heartbeats, DB pool use and data-source health. Run `docker compose --profile observability up` to get Prometheus on :9090 and Grafana on :3000.
- **History:** the `turns` table, and the SQL views `turn_stats_hourly` and `slo_daily` (migration 0019) that Grafana or Metabase can read directly.
- **Weekly:** `python scripts/slo_report.py`, the SLO and burn-rate report.

Alerts on these numbers are defined in OBS-04 (`docs/RUNBOOK.md`).
