"""
scripts/feedback_triage.py — weekly review of thumbs-down answers (LLM-12).

    python scripts/feedback_triage.py                   # untriaged negatives, last 7 days
    python scripts/feedback_triage.py --days 30 --all   # include already-triaged ones
    python scripts/feedback_triage.py --export 12,15    # confirmed bad answers -> golden cases

Each item shows the question, the answer, the user's reason, the turn's
intent/status and the Error-ID to open its trace (scripts/find_turn.py).
`--export` appends a golden-case stub per confirmed item to
evals/cases/feedback.jsonl (fill in "expect", then run evals/build_golden.py)
and marks those items triaged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATH = ROOT / "evals" / "cases" / "feedback.jsonl"


def golden_stub(item: dict) -> dict:
    """A golden-set case skeleton for one confirmed bad answer."""
    return {
        "id": f"feedback-{item['id']}",
        "domain": item.get("intent") or "unknown",
        "query": item.get("question") or "",
        "source": {"turn_id": item["turn_id"], "reason": item.get("reason"), "note": item.get("free_text")},
        "expect": {"intent": item.get("intent"), "notes": "fill in: what a correct answer must do"},
    }


def export(items: list[dict], ids: list[int], path: Path = EXPORT_PATH) -> int:
    chosen = [i for i in items if i["id"] in ids]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for item in chosen:
            f.write(json.dumps(golden_stub(item), ensure_ascii=False) + "\n")
    return len(chosen)


def main(argv: list[str] | None = None) -> int:
    from utils.db import feedback_mark_triaged, feedback_negative_since

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all", action="store_true", help="include items already triaged")
    ap.add_argument("--export", default="", help="comma-separated feedback ids to export as golden cases")
    args = ap.parse_args(argv)

    items = feedback_negative_since(datetime.now(UTC) - timedelta(days=args.days), include_triaged=args.all)
    if args.export:
        ids = [int(x) for x in args.export.split(",") if x.strip().isdigit()]
        n = export(items, ids)
        feedback_mark_triaged(ids)
        print(f"Exported {n} case(s) to {EXPORT_PATH}")
        return 0
    if not items:
        print("No thumbs-down feedback to triage.")
        return 0
    for item in items:
        print("=" * 72)
        print(f"#{item['id']}  {item['created_at']:%Y-%m-%d %H:%M}  reason: {item.get('reason') or '-'}")
        print(
            f"intent={item.get('intent')} status={item.get('status')} Error-ID={item['turn_id'].replace('-', '')[:8]}"
        )
        print(f"Q: {(item.get('question') or '?')[:300]}")
        print(f"A: {(item.get('answer') or '?')[:600]}")
        if item.get("free_text"):
            print(f"User says: {item['free_text'][:500]}")
    print("=" * 72)
    print("Confirm bad answers with: python scripts/feedback_triage.py --export <ids>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
