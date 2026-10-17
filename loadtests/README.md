# Load, soak and chaos tests (TEST-06)

A repeatable profile against ARIA with a fake LLM and fake market data whose latency looks like the real thing, a mode that breaks things while it runs, and a report that says whether the PRD's targets were met.

```
pip install -r loadtests/requirements.txt          # locust, psutil: only needed here
python loadtests/run.py --database-url postgresql://aria:aria@localhost:55432/aria_load \
    --users 20 --duration 300 --version v1.2.0
```

Use a **throwaway** database (the run migrates it and signs up `load+…@example.com` users). `run.py` starts the app itself, so nothing else has to be running.

| Piece | What it does |
|---|---|
| `run.py` | One command: migrate and seed, start the app with the fakes, warm up, one-user baseline, N-user load, sample memory and CPU, optional chaos, report. |
| `locustfile.py` | The profile: 60% equity, 15% mutual funds, 10% screener, 10% alerts, 5% reports, through the real Gradio queue API. Records time to first answer text (TTFT) and the whole turn, per kind. |
| `gradio_http.py` | A small client for Gradio's queue protocol. `gradio_client` never returns a result under Locust's gevent, so this talks plain HTTP. |
| `chaos.py` | A timeline of faults, e.g. `--chaos "60:slow_llm:45,150:upstream_500:45,240:db_restart" --db-container aria-test-pg`. Scenarios: `slow_llm`, `flaky_llm` (30% of LLM calls fail), `upstream_500` (50% of market-data calls raise), `db_restart`. |
| `memwatch.py` | Samples the app's memory (a PID or a Docker container) for a soak. |
| `report.py` | Reads Locust's CSVs, the memory series and the chaos log; checks the targets; writes `report.md` and `report.json`; exits 1 if a target is missed. |

The faults reach the app through `utils/faults.py`: a JSON file named by `ARIA_FAULTS_FILE` that only the fake LLM and the fake data read (both refuse to start in production). `chaos.py` edits that file while the load runs.

## Targets it judges

| Target | Source |
|---|---|
| turn error rate < 1% | PRD product targets |
| equity time to first token: p50 <= 6 s, p95 <= 15 s | PRD |
| report turn p95 <= 60 s | PRD |
| 20 concurrent users: equity TTFT p95 within 1.5x of one user (REL-01) | PRD |
| memory flat over a soak: < 5% growth per hour, judged only for runs of 30 minutes or more | TEST-06 |

A run with a chaos timeline does not judge latency (the faults are the point). It judges that the server was ready again within 60 s of every fault, and reports how many turns failed or came back naming missing data.

## The 2-hour soak

```
python loadtests/run.py --database-url ... --users 20 --duration 7200 --no-chaos --version v1.2.0
```

The report then adds the memory verdict. The `loadtest` workflow runs the same thing in CI (`soak: true` makes it two hours) and uploads the report as an artifact.

Latest numbers and what they showed: `docs/load/`.
