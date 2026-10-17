"""
loadtests/run.py — one command: start ARIA, load it, break it, report (TEST-06).

    python loadtests/run.py --database-url postgresql://aria:aria@localhost:55432/aria_test \\
        --users 20 --duration 300 --version v1.2.0 \\
        --chaos "60:slow_llm:45,150:upstream_500:45,240:db_restart" --db-container aria-test-pg

    python loadtests/run.py ... --duration 7200 --no-chaos      # the 2-hour soak

It runs, in order, against a THROWAWAY database (never production):
  1. migrate the database and seed the screener snapshot (offline data);
  2. start the app with the fake LLM and fake data, whose latency is set to be
     realistic (LLM call 400 ms, first streamed token 600 ms, data call 150 ms)
     through a faults file that the chaos run can change while it is going;
  3. a one-user warm-up and baseline (60 s) and then --users concurrent users for --duration
     seconds with Locust (loadtests/locustfile.py), sampling the app's memory;
  4. optionally the chaos timeline (loadtests/chaos.py) at the same time;
  5. loadtests/report.py: the report (md + json) in --out, exit 1 if a target is missed.

The Locust and psutil packages are only needed here: pip install -r loadtests/requirements.txt
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess  # nosec B404 - starts this repo's own server and tools
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SEED = (
    "from scripts.refresh_fundamentals import refresh; from tools.data.providers.fake import FakeProvider; "
    "refresh([(s, s, '') for s in ['TCS','INFY','WIPRO','HCLTECH','TECHM','RELIANCE','HDFCBANK','ICICIBANK','SBIN','ITC']], "
    "workers=1, delay=0, fetch=FakeProvider().quote)"
)
REALISTIC = {"llm_latency_ms": 400, "llm_first_token_ms": 600, "data_latency_ms": 150}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def server_env(database_url: str, port: int, faults_file: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "ARIA_NO_DOTENV": "1",
        "APP_ENV": "dev",
        "DATABASE_URL": database_url,
        "SECRET_KEY": "loadtest-secret-key-0123456789-abcdefghijkl",
        "ARIA_FAKE_LLM": "1",
        "ARIA_FAKE_DATA": "1",
        "ARIA_FAULTS_FILE": str(faults_file),
        "RUN_JOBS_IN_APP": "false",
        "MAIL_BACKEND": "console",
        "REQUIRE_EMAIL_VERIFICATION": "false",
        "AUTH_IP_MAX_ATTEMPTS": "100000",
        "TURNS_PER_MINUTE": "600",
        "TRIAL_DAILY_LIMIT": "100000",
        "CHAT_CONCURRENCY": "32",
        "PORT": str(port),
        "PYTHONUTF8": "1",
    }
    return env


def wait_ready(url: str, timeout: float = 120.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with urllib.request.urlopen(url + "/readyz", timeout=5) as r:  # noqa: S310  # nosec B310
                if r.status == 200:
                    return
        except Exception:  # noqa: S110
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} never became ready")


def locust(url: str, users: int, seconds: int, prefix: Path, spawn_rate: float) -> None:
    subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-m",
            "locust",
            "-f",
            str(ROOT / "loadtests" / "locustfile.py"),
            "--headless",
            "-u",
            str(users),
            "-r",
            str(spawn_rate),
            "-t",
            f"{seconds}s",
            "--host",
            url,
            "--csv",
            str(prefix),
            "--only-summary",
        ],  # fmt: skip
        check=False,
        cwd=ROOT,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--baseline-seconds", type=int, default=60)
    ap.add_argument(
        "--warmup-seconds", type=int, default=20, help="one user, results discarded, before the baseline"
    )
    ap.add_argument("--version", default="dev")
    ap.add_argument("--out", type=Path, default=ROOT / "test-results" / "load")
    ap.add_argument("--chaos", default="")
    ap.add_argument("--no-chaos", action="store_true")
    ap.add_argument("--db-container", default="")
    ap.add_argument("--mem-every", type=float, default=15)
    args = ap.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):  # a report must describe this run only
        if stale.is_file():
            stale.unlink()
    faults = out / "faults.json"
    faults.write_text(json.dumps(REALISTIC), encoding="utf-8")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = server_env(args.database_url, port, faults)

    subprocess.run([sys.executable, "scripts/migrate.py"], check=True, cwd=ROOT, env=env)  # noqa: S603  # nosec B603
    subprocess.run([sys.executable, "-c", SEED], check=True, cwd=ROOT, env=env)  # noqa: S603  # nosec B603
    log = (out / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "app.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )  # noqa: S603  # nosec B603
    try:
        wait_ready(url)
        print(f"ARIA is up at {url} (pid {server.pid})", flush=True)  # noqa: T201

        if args.warmup_seconds:  # caches and connection pools warm, so the baseline is a steady state
            locust(url, 1, args.warmup_seconds, out / "warmup", 1)
        locust(url, 1, args.baseline_seconds, out / "baseline", 1)

        from loadtests import memwatch

        stop = threading.Event()

        def sample() -> None:
            start = time.monotonic()
            with (out / "mem.csv").open("w", encoding="utf-8") as f:
                f.write("elapsed_s,rss_mb,cpu_pct\n")
                while not stop.is_set():
                    try:
                        f.write(
                            f"{time.monotonic() - start:.0f},{memwatch.rss_mb_of_pid(server.pid):.1f},"
                            f"{memwatch.cpu_pct_of_pid(server.pid):.0f}\n"
                        )
                        f.flush()
                    except Exception:  # noqa: S110 - the process may be restarting
                        pass
                    stop.wait(args.mem_every)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()

        chaos_thread = None
        if args.chaos and not args.no_chaos:
            from loadtests import chaos

            timeline = chaos.parse_timeline(args.chaos)
            chaos_thread = threading.Thread(
                target=chaos.run,
                args=(timeline, faults, url, args.db_container, out / "chaos.json"),
                daemon=True,
            )
            chaos_thread.start()

        locust(url, args.users, args.duration, out / "run", spawn_rate=max(1, args.users / 10))
        if chaos_thread:
            chaos_thread.join(timeout=300)
        stop.set()
        sampler.join(timeout=5)
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    from loadtests import report

    argv_report = ["--run", str(out / "run"), "--baseline", str(out / "baseline"), "--mem", str(out / "mem.csv"),
                   "--version", args.version, "--out", str(out / "report"),
                   "--users", f"{args.users} concurrent users, {args.duration} s"]  # fmt: skip
    if (out / "chaos.json").exists():
        argv_report += ["--chaos", str(out / "chaos.json")]
    return report.main(argv_report)


if __name__ == "__main__":
    raise SystemExit(main())
