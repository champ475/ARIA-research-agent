"""
loadtests/chaos.py — inject faults while a load run is going (TEST-06).

    python loadtests/chaos.py --faults-file test-results/load/faults.json \\
        --timeline "60:slow_llm:45,150:upstream_500:45,240:db_restart" \\
        --db-container aria-test-pg --url http://localhost:7870

The timeline is `start_seconds:scenario[:duration_seconds]`, comma separated:

    slow_llm        every LLM call waits 3 s and a streamed answer's first token 5 s more
    flaky_llm       30% of LLM calls fail (the app should fall back and still answer)
    upstream_500    50% of market-data calls raise like an upstream 500
    db_restart      `docker restart <db container>`; the app must recover on its own

The server must run with ARIA_FAULTS_FILE pointing at --faults-file (and the
fake LLM / fake data, which are the only code that reads it). After each
scenario this waits for /readyz and writes events to --log so the report can
line them up with the load graph. It never touches a real provider.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - runs `docker restart` on a container the operator names
import sys
import time
import urllib.request
from pathlib import Path

SCENARIOS = {
    "slow_llm": {"llm_latency_ms": 3000, "llm_first_token_ms": 5000},
    "flaky_llm": {"llm_error_rate": 0.3},
    "upstream_500": {"data_error_rate": 0.5},
}


def parse_timeline(text: str) -> list[tuple[float, str, float]]:
    """'60:slow_llm:45,240:db_restart' -> [(60, 'slow_llm', 45), (240, 'db_restart', 0)], by start time."""
    out = []
    for part in text.split(","):
        bits = part.strip().split(":")
        if len(bits) < 2:
            raise ValueError(f"bad timeline entry {part!r}")
        name = bits[1]
        if name not in SCENARIOS and name != "db_restart":
            raise ValueError(f"unknown scenario {name!r}")
        out.append((float(bits[0]), name, float(bits[2]) if len(bits) > 2 else 0.0))
    return sorted(out)


def _ready(url: str, timeout: float = 120.0) -> float | None:
    """Seconds until GET /readyz answers 200, or None if it never does."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with urllib.request.urlopen(url + "/readyz", timeout=5) as r:  # nosec B310 - the operator's own server
                if r.status == 200:
                    return time.monotonic() - start
        except Exception:  # noqa: S110 - polling
            pass
        time.sleep(1)
    return None


def run(timeline, faults_file: Path, url: str, db_container: str, log_path: Path) -> list[dict]:
    events: list[dict] = []
    t0 = time.monotonic()
    faults_file.parent.mkdir(parents=True, exist_ok=True)
    faults_file.write_text("{}", encoding="utf-8")

    def note(**kw) -> None:
        e = {"at_s": round(time.monotonic() - t0, 1), **kw}
        events.append(e)
        log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
        print(json.dumps(e), flush=True)  # noqa: T201

    for start, name, duration in timeline:
        time.sleep(max(0.0, start - (time.monotonic() - t0)))
        if name == "db_restart":
            note(scenario=name, event="start")
            subprocess.run(["docker", "restart", db_container], check=True, capture_output=True)  # noqa: S603, S607 # nosec B603 B607
            note(scenario=name, event="restarted", ready_after_s=_ready(url))
            continue
        note(scenario=name, event="start")
        faults_file.write_text(json.dumps(SCENARIOS[name]), encoding="utf-8")
        time.sleep(duration)
        faults_file.write_text("{}", encoding="utf-8")
        note(scenario=name, event="end", ready_after_s=_ready(url))
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faults-file", type=Path, required=True)
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--url", default="http://localhost:7870")
    ap.add_argument("--db-container", default="")
    ap.add_argument("--log", type=Path, default=Path("test-results/load/chaos.json"))
    args = ap.parse_args(argv)
    timeline = parse_timeline(args.timeline)
    if any(n == "db_restart" for _, n, _ in timeline) and not args.db_container:
        print("db_restart needs --db-container", file=sys.stderr)  # noqa: T201
        return 2
    args.log.parent.mkdir(parents=True, exist_ok=True)
    run(timeline, args.faults_file, args.url, args.db_container, args.log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
