"""
scripts/slo_report.py — weekly SLO and error-budget report (OBS-03).

    python scripts/slo_report.py            # last 7 and 30 days
    python scripts/slo_report.py --json

SLOs (docs/SLO.md):
  availability   ≥ 99.5% of chat turns answered (not 'error'/'llm_unavailable'), monthly
  latency        equity answers: p95 time to first output ≤ 15 s

The error budget is the failures the availability SLO allows (0.5% of
turns). "Budget used" is failures ÷ budget over the window; above 100% the
SLO is missed. Over 7 days, using more than 7/30 of the monthly budget
means it's burning too fast.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def window(days: int, now: datetime | None = None) -> dict[str, Any]:
    from config import SLO_AVAILABILITY, SLO_EQUITY_P95_TTFT_SECONDS
    from utils.db import slo_summary

    now = now or datetime.now(UTC)
    s = slo_summary(now - timedelta(days=days))
    turns, good = int(s["turns"] or 0), int(s["good"] or 0)
    failed = turns - good
    budget = (1 - SLO_AVAILABILITY) * turns
    p95 = s["equity_p95_ttft_ms"]
    return {
        "days": days,
        "turns": turns,
        "failed": failed,
        "availability": round(good / turns, 5) if turns else None,
        "availability_ok": (good / turns >= SLO_AVAILABILITY) if turns else None,
        "budget_used": round(failed / budget, 3) if budget else (0.0 if not failed else None),
        "equity_p95_ttft_s": round(float(p95) / 1000, 2) if p95 is not None else None,
        "latency_ok": (float(p95) / 1000 <= SLO_EQUITY_P95_TTFT_SECONDS) if p95 is not None else None,
    }


def report(now: datetime | None = None) -> dict[str, Any]:
    from config import SLO_AVAILABILITY

    week, month = window(7, now), window(30, now)
    # The monthly budget refills at 7/30 per week; a week that spends more burns too fast.
    monthly_budget = (1 - SLO_AVAILABILITY) * month["turns"]
    week["burning_fast"] = week["failed"] > 7 / 30 * monthly_budget if month["turns"] else False
    return {"week": week, "month": month}


def _fmt(w: dict[str, Any]) -> str:
    avail = "—" if w["availability"] is None else f"{w['availability'] * 100:.2f}%"
    used = "—" if w["budget_used"] is None else f"{w['budget_used'] * 100:.0f}%"
    p95 = "—" if w["equity_p95_ttft_s"] is None else f"{w['equity_p95_ttft_s']} s"
    flag = lambda ok: "✅" if ok else ("❌" if ok is False else "·")  # noqa: E731
    return (
        f"last {w['days']:>2} days: {w['turns']} turns, {w['failed']} failed | availability {avail} "
        f"{flag(w['availability_ok'])} | error budget used {used} | equity p95 TTFT {p95} {flag(w['latency_ok'])}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = report()
    if args.json:
        sys.stdout.write(json.dumps(r, indent=2) + "\n")
    else:
        sys.stdout.write(_fmt(r["week"]) + "\n" + _fmt(r["month"]) + "\n")
        if r["week"].get("burning_fast"):
            sys.stdout.write("⚠️ The error budget is burning faster than it refills.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
