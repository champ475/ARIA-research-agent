"""
scripts/retention_report.py — is every table inside its retention period? (COMP-06)

    python scripts/retention_report.py            print the report; exit 1 if any class is overdue
    python scripts/retention_report.py --json     machine-readable
    python scripts/retention_report.py --purge    run the retention job first, then report

The same check runs hourly in the ops watchdog (`retention_overdue`); this is
the monthly report the policy asks for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--purge", action="store_true")
    args = ap.parse_args(argv)

    from config import check_boot
    from utils.logging_setup import configure_logging
    from utils.retention import report, run_retention

    configure_logging()
    check_boot(serving=False)
    if args.purge:
        run_retention()
    rows = report()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'class':<22} {'limit':>8} {'oldest row':>12}  status")
        for r in rows:
            oldest = "-" if r.get("age_days") is None else f"{r['age_days']:.0f} d"
            status = {True: "ok", False: "OVERDUE", None: "unreadable"}[r["ok"]]
            print(f"{r['class']:<22} {str(r['keep_days']) + ' d':>8} {oldest:>12}  {status}")
    return 0 if all(r["ok"] is not False for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
