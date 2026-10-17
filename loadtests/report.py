"""
loadtests/report.py — the per-release load report (TEST-06).

    python loadtests/report.py --run test-results/load/run \\
        --baseline test-results/load/baseline \\
        --mem test-results/load/mem.csv --chaos test-results/load/chaos.json \\
        --version v1.2.0 --out test-results/load/report

Reads Locust's `<prefix>_stats.csv` files (the concurrent run, and a
single-user baseline), an optional memory series and chaos event log, checks
them against the PRD's product targets and writes `report.md` and `report.json`.
Exit code 1 if any assessed target is missed, so a release can gate on it.

Targets (PRD "Product-level targets"):
  turn error rate           < 1% of turns
  equity time to first token  p50 <= 6 s and p95 <= 15 s
  report turn latency       p95 <= 60 s
  20 concurrent users       equity TTFT p95 within 1.5x the single-user p95
  memory (soak, >= 30 min)  grows by < 5% of its mean per hour (flat)

With a chaos log (faults injected) the latency targets are shown but not judged; the check
is that the server was ready again within 60 s of every fault.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

MIN_SOAK_SECONDS = 30 * 60
MAX_ERROR_RATE = 0.01
MAX_GROWTH_PER_HOUR = 0.05
RECOVERY_SECONDS = 60


def read_stats(prefix: Path) -> dict[tuple[str, str], dict[str, float]]:
    """{(type, name): {count, failures, p50, p95}} from `<prefix>_stats.csv` (ms)."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    path = prefix.with_name(prefix.name + "_stats.csv")
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["Name"] == "Aggregated":
                continue
            out[(row["Type"], row["Name"])] = {
                "count": float(row["Request Count"]),
                "failures": float(row["Failure Count"]),
                "p50": float(row["50%"] or 0),
                "p95": float(row["95%"] or 0),
            }
    return out


def read_memory(path: Path) -> list[tuple[float, float]]:
    pts = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("rss_mb"):
                pts.append((float(row["elapsed_s"]), float(row["rss_mb"])))
    return pts


def read_cpu(path: Path) -> list[float]:
    out = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("cpu_pct"):
                out.append(float(row["cpu_pct"]))
    return out


def memory_trend(points: list[tuple[float, float]]) -> dict[str, Any]:
    """Least-squares growth of the memory series, as MB per hour and as a share of its mean."""
    if len(points) < 3:
        return {"assessed": False, "reason": "fewer than 3 samples"}
    span = points[-1][0] - points[0][0]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope_per_s = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom if denom else 0.0
    per_hour = slope_per_s * 3600
    share = per_hour / my if my else 0.0
    if span < MIN_SOAK_SECONDS:
        return {
            "assessed": False,
            "reason": f"run was {span / 60:.0f} min; a soak verdict needs 30+",
            "mb_per_hour": per_hour,
            "samples": len(points),
        }
    return {
        "assessed": True,
        "mb_per_hour": per_hour,
        "share_of_mean_per_hour": share,
        "start_mb": ys[0],
        "end_mb": ys[-1],
        "peak_mb": max(ys),
        "ok": share < MAX_GROWTH_PER_HOUR,
        "samples": len(points),
    }


def evaluate(
    run: dict[tuple[str, str], dict[str, float]],
    baseline: dict[tuple[str, str], dict[str, float]] | None,
    memory: list[tuple[float, float]] | None,
    chaos: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Latency targets are judged on a clean run. When faults were injected the latency
    figures describe the faults, so they are shown but not judged; what is judged is
    that the server came back after every fault."""
    checks: list[dict[str, Any]] = []
    turns = {k[1]: v for k, v in run.items() if k[0] == "TURN"}
    ttft = {k[1]: v for k, v in run.items() if k[0] == "TTFT"}
    total = sum(v["count"] for v in turns.values())
    failed = sum(v["failures"] for v in turns.values())
    rate = failed / total if total else 1.0
    measured = f"{rate:.2%} ({int(failed)}/{int(total)})"
    if chaos:
        ended = [e for e in chaos if e["event"] in ("end", "restarted")]
        slow = [e for e in ended if e.get("ready_after_s") is None or e["ready_after_s"] > RECOVERY_SECONDS]
        checks.append(
            {
                "name": f"server ready again within {RECOVERY_SECONDS} s after every injected fault",
                "value": f"{len(ended) - len(slow)}/{len(ended)} recovered in time",
                "ok": bool(ended) and not slow,
            }
        )
        checks.append(
            {"name": "turn failures while faults were on (informational)", "value": measured, "ok": None}
        )
        degraded = sum(v["count"] for k, v in run.items() if k[0] == "DEGRADED")
        checks.append(
            {
                "name": "answers that named missing data (informational)",
                "value": f"{int(degraded)} of {int(total)} turns",
                "ok": None,
            }
        )
        checks.append(
            {"name": "latency targets", "value": "not judged: faults were injected in this run", "ok": None}
        )
    else:
        checks.append(
            {"name": "turn error rate < 1%", "value": measured, "ok": total > 0 and rate < MAX_ERROR_RATE}
        )
        if "equity" in ttft:
            e = ttft["equity"]
            checks.append(
                {
                    "name": "equity TTFT p50 <= 6 s",
                    "value": f"{e['p50'] / 1000:.2f} s",
                    "ok": e["p50"] <= 6000,
                }
            )
            checks.append(
                {
                    "name": "equity TTFT p95 <= 15 s",
                    "value": f"{e['p95'] / 1000:.2f} s",
                    "ok": e["p95"] <= 15000,
                }
            )
        if "report" in turns and turns["report"]["count"]:
            r = turns["report"]
            checks.append(
                {
                    "name": "report turn p95 <= 60 s",
                    "value": f"{r['p95'] / 1000:.2f} s",
                    "ok": r["p95"] <= 60000,
                }
            )
        if baseline and ("TTFT", "equity") in baseline and "equity" in ttft:
            single = baseline[("TTFT", "equity")]["p95"]
            conc = ttft["equity"]["p95"]
            ratio = conc / single if single else float("inf")
            checks.append(
                {
                    "name": "concurrent equity TTFT p95 within 1.5x single-user",
                    "value": f"{ratio:.2f}x ({conc / 1000:.2f} s vs {single / 1000:.2f} s)",
                    "ok": ratio <= 1.5,
                }
            )
    if memory:
        t = memory_trend(memory)
        if t["assessed"]:
            checks.append(
                {
                    "name": "memory flat over the soak (< 5% growth/hour)",
                    "value": f"{t['mb_per_hour']:+.1f} MB/h ({t['share_of_mean_per_hour']:+.1%}/h), {t['start_mb']:.0f} to {t['end_mb']:.0f} MB",
                    "ok": t["ok"],
                }
            )
        else:
            checks.append(
                {"name": "memory flat over the soak", "value": f"not assessed: {t['reason']}", "ok": None}
            )
    return checks


def render(
    version: str, checks: list[dict[str, Any]], run: dict, chaos: list[dict] | None, users: str
) -> str:
    mark = {True: "PASS", False: "FAIL", None: "n/a"}
    lines = [
        f"# ARIA load report: {version}",
        "",
        f"Profile: {users}",
        "",
        "## Targets",
        "",
        "| Target | Measured | Result |",
        "|---|---|---|",
    ]
    lines += [f"| {c['name']} | {c['value']} | {mark[c['ok']]} |" for c in checks]
    lines += [
        "",
        "## By kind of question",
        "",
        "| Kind | Turns | Failures | TTFT p50 | TTFT p95 | Turn p95 |",
        "|---|---|---|---|---|---|",
    ]
    kinds = sorted({k[1] for k in run})
    for kind in kinds:
        t, f = run.get(("TURN", kind)), run.get(("TTFT", kind))
        if t and f:
            lines.append(
                f"| {kind} | {int(t['count'])} | {int(t['failures'])} | {f['p50'] / 1000:.2f} s | {f['p95'] / 1000:.2f} s | {t['p95'] / 1000:.2f} s |"
            )
    if chaos:
        lines += [
            "",
            "## Faults injected",
            "",
            "| At (s) | Scenario | Event | Server ready again after |",
            "|---|---|---|---|",
        ]
        for e in chaos:
            ready = e.get("ready_after_s")
            lines.append(
                f"| {e['at_s']} | {e['scenario']} | {e['event']} | {'-' if ready is None and e['event'] == 'start' else (f'{ready:.1f} s' if ready is not None else 'NEVER')} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--mem", type=Path)
    ap.add_argument("--chaos", type=Path)
    ap.add_argument("--version", default="dev")
    ap.add_argument(
        "--users", default="20 concurrent users; 60% equity, 15% fund, 10% screener, 10% alerts, 5% report"
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    run = read_stats(args.run)
    baseline = read_stats(args.baseline) if args.baseline else None
    memory = read_memory(args.mem) if args.mem else None
    chaos = json.loads(args.chaos.read_text(encoding="utf-8")) if args.chaos and args.chaos.exists() else None
    checks = evaluate(run, baseline, memory, chaos)
    cpu = read_cpu(args.mem) if args.mem else []
    if len(cpu) > 2:  # the first sample is the CPU since start-up, not under load
        busy = cpu[2:]
        checks.append(
            {
                "name": "server CPU (informational; 100 = one core)",
                "value": f"mean {statistics.fmean(busy):.0f}, peak {max(busy):.0f}",
                "ok": None,
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".md").write_text(
        render(args.version, checks, run, chaos, args.users), encoding="utf-8"
    )
    args.out.with_suffix(".json").write_text(
        json.dumps({"version": args.version, "checks": checks, "chaos": chaos}, indent=2), encoding="utf-8"
    )
    print(args.out.with_suffix(".md").read_text(encoding="utf-8"))  # noqa: T201
    return 0 if all(c["ok"] is not False for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
