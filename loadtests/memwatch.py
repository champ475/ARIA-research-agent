"""
loadtests/memwatch.py — sample the server's memory during a soak (TEST-06).

    python loadtests/memwatch.py --pid 12345 --out test-results/load/mem.csv --seconds 7200
    python loadtests/memwatch.py --docker aria-app-1 --out test-results/load/mem.csv --seconds 7200

Writes `elapsed_s,rss_mb` every --every seconds (default 30). With --pid it
sums the process and its children (psutil). With --docker it reads the
container's memory from `docker stats`. loadtests/report.py turns the series
into a growth rate and says whether memory was flat.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - `docker stats` on a container the operator names
import time
from pathlib import Path


def rss_mb_of_pid(pid: int) -> float:
    import psutil

    proc = psutil.Process(pid)
    total = proc.memory_info().rss + sum(c.memory_info().rss for c in proc.children(recursive=True))
    return total / 1024 / 1024


_procs: dict[int, object] = {}


def cpu_pct_of_pid(pid: int) -> float:
    """CPU use of the process and its children since the last call, in percent of one core.
    psutil measures between two calls on the same Process object, so those are kept."""
    import psutil

    proc = _procs.setdefault(pid, psutil.Process(pid))
    total = proc.cpu_percent(interval=None)
    for child in proc.children(recursive=True):
        total += _procs.setdefault(child.pid, child).cpu_percent(interval=None)
    return total


def rss_mb_of_container(name: str) -> float:
    out = (
        subprocess.run(  # noqa: S603  # nosec B603 B607
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.split("/")[0]
        .strip()
    )
    units = {
        "KiB": 1 / 1024,
        "MiB": 1.0,
        "GiB": 1024.0,
        "kB": 1 / 1000,
        "MB": 1.0,
        "GB": 1000.0,
        "B": 1 / 1024 / 1024,
    }
    for unit in sorted(units, key=len, reverse=True):
        if out.endswith(unit):
            return float(out[: -len(unit)]) * units[unit]
    raise ValueError(f"cannot read memory from {out!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pid", type=int)
    src.add_argument("--docker")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=7200)
    ap.add_argument("--every", type=float, default=30)
    args = ap.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with args.out.open("w", encoding="utf-8") as f:
        f.write("elapsed_s,rss_mb\n")
        while (elapsed := time.monotonic() - start) < args.seconds:
            try:
                mb = rss_mb_of_pid(args.pid) if args.pid else rss_mb_of_container(args.docker)
                f.write(f"{elapsed:.0f},{mb:.1f}\n")
                f.flush()
            except Exception as e:  # noqa: BLE001 - keep sampling through a restart
                f.write(f"{elapsed:.0f},\n")
                f.flush()
                print(f"sample failed: {type(e).__name__}")  # noqa: T201
            time.sleep(args.every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
