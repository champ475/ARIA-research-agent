"""
evals/judge_calibration.py — measure the verification judge (LLM-13).

Runs stage 3 of graph/nodes/verification.py (`run_judge`) over the 100
labelled pairs in evals/cases/judge_labels.jsonl and reports, treating "the
judge raised an issue" as the positive class:

  precision  flagged cases that really were insufficient
  recall     insufficient cases the judge flagged
  gate rate  share of *sufficient* cases that would pop the VerificationGate
             (the false-positive rate; the target is < 5% of turns)

Targets: precision >= 0.80, recall >= 0.70, gate rate < 0.05.

  python evals/judge_calibration.py            replay recorded replies (no network)
  python evals/judge_calibration.py --record   fill missing replies from the configured LLM chain
  python evals/judge_calibration.py --strong   score the synthesis-class model instead of the fast one
  python evals/judge_calibration.py --enforce  exit 1 if a target is missed

Replies are cached in evals/fixtures/judge.jsonl, keyed by (node, system, prompt),
so changing the judge prompt or the evidence shape means re-recording.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.runner import FixtureStore, MissingFixture, RecordReplayClient  # noqa: E402

LABELS = ROOT / "evals" / "cases" / "judge_labels.jsonl"
FIXTURES = ROOT / "evals" / "fixtures" / "judge.jsonl"
MIN_PRECISION = 0.80
MIN_RECALL = 0.70
MAX_GATE_RATE = 0.05


def load_labels(path: Path = LABELS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score(rows: list[dict]) -> dict:
    """rows: [{'label', 'family', 'flagged'}] for the cases the judge answered."""
    tp = sum(r["flagged"] and r["label"] == "insufficient" for r in rows)
    fp = sum(r["flagged"] and r["label"] == "sufficient" for r in rows)
    fn = sum(not r["flagged"] and r["label"] == "insufficient" for r in rows)
    tn = sum(not r["flagged"] and r["label"] == "sufficient" for r in rows)
    out = {
        "n": len(rows),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "gate_rate": fp / (fp + tn) if fp + tn else None,
    }
    fams: dict[str, list[int]] = {}
    for r in rows:
        if r["label"] == "insufficient":
            f = fams.setdefault(r["family"], [0, 0])
            f[0] += r["flagged"]
            f[1] += 1
    out["recall_by_family"] = {k: f"{a}/{b}" for k, (a, b) in sorted(fams.items())}
    return out


def meets_targets(m: dict) -> list[str]:
    misses = []
    if m["precision"] is None or m["precision"] < MIN_PRECISION:
        misses.append(f"precision {m['precision']} < {MIN_PRECISION}")
    if m["recall"] is None or m["recall"] < MIN_RECALL:
        misses.append(f"recall {m['recall']} < {MIN_RECALL}")
    if m["gate_rate"] is None or m["gate_rate"] >= MAX_GATE_RATE:
        misses.append(f"gate rate {m['gate_rate']} >= {MAX_GATE_RATE}")
    return misses


def run(labels: list[dict], client) -> tuple[list[dict], int]:
    from graph.nodes.verification import run_judge

    rows, unrecorded = [], 0
    for case in labels:
        try:
            issues = run_judge(client, case["objective"], case["evidence_summary"])
        except MissingFixture:
            unrecorded += 1
            continue
        rows.append(
            {"label": case["label"], "family": case["family"], "flagged": bool(issues), "id": case["id"]}
        )
    return rows, unrecorded


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--strong", action="store_true")
    ap.add_argument("--enforce", action="store_true")
    args = ap.parse_args(argv)

    from graph.nodes import verification

    node = "verification_complex" if args.strong else "verification"
    client = RecordReplayClient(
        node, verification._SYSTEM, FixtureStore(FIXTURES), "record" if args.record else "replay"
    )
    rows, unrecorded = run(load_labels(), client)
    if not rows:
        print(
            f"no recorded judge replies ({unrecorded} cases unrecorded) — run with --record and working LLM keys"
        )
        return 0
    m = score(rows)
    print(json.dumps({**m, "unrecorded": unrecorded}, indent=2))
    misses = meets_targets(m)
    for miss in misses:
        print("MISS:", miss)
    return 1 if (misses and args.enforce and not unrecorded) else 0


if __name__ == "__main__":
    raise SystemExit(main())
