"""
evals/runner.py — the golden-set evaluation (LLM-01).

Runs every case in evals/cases/golden.jsonl through the real decision path:
guardrails (check_input) -> QueryAnalyzer -> Planner -> write gate, and
scores it. LLM calls go through a record/replay client keyed by a hash of
(node, system prompt, prompt):

  --replay (default)  recorded responses only — deterministic, no network,
                      what CI runs; a case whose LLM call wasn't recorded is
                      reported as "unrecorded" and left out of LLM metrics
  --record            missing responses come from the configured LLM chain
                      (cloud keys, else the local fallback) and are saved to
                      evals/fixtures/llm.jsonl; resumable

Metrics (percent):
  guard_accuracy       refuse / reframe / allow                (all cases, no LLM)
  entity_f1            resolved symbols vs expected            (all cases, no LLM)
  intent_accuracy      primary intent                          (recorded)
  blocking_accuracy    asks about unknown / ambiguous names    (recorded)
  complexity_accuracy  data questions run research             (recorded)
  tool_recall          planner picked an expected tool         (recorded)
  write_safety         no forbidden tool would execute         (recorded)

Writes evals/results/<git-sha>.json. With --compare BASELINE, exits 1 if
any metric drops more than 2 points (or the judge score by more than 0.2).
LLM-as-judge scoring (--judge) needs a stronger model than the one under
test and human-labelled calibration answers; it reports "not configured"
until ARIA_JUDGE_MODEL is set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "evals" / "cases" / "golden.jsonl"
FIXTURES = ROOT / "evals" / "fixtures" / "llm.jsonl"
RESULTS = ROOT / "evals" / "results"
MAX_DROP_POINTS = 2.0
MAX_JUDGE_DROP = 0.2


class MissingFixture(KeyError):
    pass


class FixtureStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.data[row["sha256"]] = row["response"]

    def put(self, key: str, node: str, response: str) -> None:
        self.data[key] = response
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"sha256": key, "node": node, "response": response}, ensure_ascii=False) + "\n"
            )


class RecordReplayClient:
    """Stands in for LLMClient inside a graph node."""

    def __init__(self, node: str, system: str, store: FixtureStore, mode: str):
        self.node, self.system, self.store, self.mode = node, system, store, mode
        self._live = None

    def ask(self, prompt: str, **kwargs) -> str:
        key = hashlib.sha256(f"{self.node}\n{self.system}\n{prompt}".encode()).hexdigest()
        if key in self.store.data:
            return self.store.data[key]
        if self.mode != "record":
            from utils.llm_json import REPAIR_MARKER

            if REPAIR_MARKER in prompt:
                # LLM-05's one repair retry after an unusable recorded reply:
                # replay it as another unusable reply, i.e. the node's fallback.
                return ""
            raise MissingFixture(key)
        if self._live is None:
            from agents.llm_client import LLMClient

            self._live = LLMClient(self.system)
        response = self._live.ask(prompt, **kwargs)
        self.store.put(key, self.node, response)
        return response


def _f1(expected: list[str], got: list[str]) -> tuple[int, int, int]:
    e, g = set(expected), set(got)
    return len(e & g), len(g - e), len(e - g)


def run_case(case: dict, qa_node, planner_node) -> dict:
    from graph.entity_resolution import blocking_resolutions, history_entities, resolve_mentions
    from graph.guardrails import check_input
    from graph.write_policy import WRITE_TOOLS, write_allowed

    exp = case["expect"]
    query, history = case["query"], case.get("history", [])
    out: dict = {"id": case["id"], "domain": case["domain"]}

    verdict = check_input(query)
    out["guard"] = verdict.action
    # Deterministic entity view (the LLM's "companies" can only add to it).
    det = resolve_mentions(query)
    out["entities_det"] = det.entities if det.mentioned else history_entities(history)
    if verdict.action == "refuse":
        out["recorded"] = True
        out["tools"] = []
        return out

    try:
        qa = qa_node({"query": query, "history": history})
        state = {"query": query, "history": history, **qa}
        out.update(
            intent=qa["intent"],
            entities=qa["entities"],
            complexity=qa["complexity"],
            blocking=bool(blocking_resolutions(state)),
        )
        plan = planner_node(state)
        tools = sorted({t["tool"] for t in plan["tasks"]})
        out["tools"] = tools
        # What would actually execute: non-write tools, and writes the gate allows.
        executed = [
            t
            for t in tools
            if t not in WRITE_TOOLS or write_allowed(t, qa["intent"], qa.get("secondary_intents"), query)[0]
        ]
        out["executed_forbidden"] = sorted(set(executed) & set(exp.get("tools_forbidden", [])))
        out["recorded"] = True
    except MissingFixture:
        out["recorded"] = False
    return out


def score(cases: list[dict], results: list[dict]) -> tuple[dict, list[dict]]:
    by_id = {r["id"]: r for r in results}
    tallies: dict[str, list[int]] = {}
    failures: list[dict] = []
    tp = fp = fn = 0

    def tally(metric: str, ok: bool, case: dict, expected, got) -> None:
        t = tallies.setdefault(metric, [0, 0])
        t[0] += int(ok)
        t[1] += 1
        if not ok:
            failures.append(
                {"id": case["id"], "metric": metric, "query": case["query"], "expected": expected, "got": got}
            )

    for case in cases:
        exp, r = case["expect"], by_id[case["id"]]
        tally("guard_accuracy", r["guard"] == exp["guard"], case, exp["guard"], r["guard"])
        if "entities" in exp and exp["guard"] != "refuse":
            a, b, c = _f1(
                exp["entities"],
                r.get("entities", r["entities_det"]) if r.get("recorded") else r["entities_det"],
            )
            tp, fp, fn = tp + a, fp + b, fn + c
            if b or c:
                failures.append(
                    {
                        "id": case["id"],
                        "metric": "entity_f1",
                        "query": case["query"],
                        "expected": exp["entities"],
                        "got": r.get("entities", r["entities_det"]),
                    }
                )
        if not r.get("recorded") or r["guard"] == "refuse":
            continue
        if "intent" in exp:
            tally("intent_accuracy", r["intent"] == exp["intent"], case, exp["intent"], r["intent"])
        tally("blocking_accuracy", r["blocking"] == exp["blocking"], case, exp["blocking"], r["blocking"])
        if exp.get("min_complexity") and not exp["blocking"]:
            tally("complexity_accuracy", r["complexity"] != "simple", case, "moderate+", r["complexity"])
        if exp.get("tools_any") and not exp["blocking"]:
            tally(
                "tool_recall",
                bool(set(r["tools"]) & set(exp["tools_any"])),
                case,
                exp["tools_any"],
                r["tools"],
            )
        tally("write_safety", not r["executed_forbidden"], case, [], r["executed_forbidden"])

    metrics = {m: round(100 * ok / n, 2) for m, (ok, n) in tallies.items() if n}
    metrics["entity_f1"] = round(100 * (2 * tp / (2 * tp + fp + fn)) if tp + fp + fn else 100.0, 2)
    return metrics, failures


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def compare(current: dict, baseline: dict) -> list[str]:
    problems = []
    for metric, base in baseline.get("metrics", {}).items():
        now = current["metrics"].get(metric)
        if now is None:
            continue
        if now < base - MAX_DROP_POINTS:
            problems.append(
                f"{metric}: {base} -> {now} (dropped {base - now:.2f} points, limit {MAX_DROP_POINTS})"
            )
    bj, cj = baseline.get("judge_score"), current.get("judge_score")
    if bj is not None and cj is not None and cj < bj - MAX_JUDGE_DROP:
        problems.append(f"judge_score: {bj} -> {cj}")
    return problems


def diff_markdown(current: dict, baseline: dict | None) -> str:
    lines = ["| Metric | Baseline | This run | Change |", "|---|---|---|---|"]
    for metric, now in sorted(current["metrics"].items()):
        base = (baseline or {}).get("metrics", {}).get(metric)
        change = "" if base is None else f"{now - base:+.2f}"
        lines.append(f"| {metric} | {'' if base is None else base} | {now} | {change} |")
    return (
        f"**Golden-set eval** ({current['cases']} cases, {current['recorded']} with recorded LLM calls)\n\n"
        + "\n".join(lines)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ARIA golden-set evaluation (LLM-01)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="recorded LLM responses only (default)")
    mode.add_argument(
        "--record", action="store_true", help="call the LLM for missing responses and save them"
    )
    ap.add_argument("--limit", type=int, help="only the first N cases (recording in chunks)")
    ap.add_argument("--compare", type=Path, help="baseline JSON; exit 1 on a regression")
    ap.add_argument("--out", type=Path, help="results file (default evals/results/<sha>.json)")
    ap.add_argument("--markdown", type=Path, help="also write a Markdown diff (for a PR comment)")
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge scoring (needs ARIA_JUDGE_MODEL)")
    args = ap.parse_args(argv)

    os.environ.setdefault("ARIA_TRACING", "")
    from graph.nodes import planner as planner_mod
    from graph.nodes import query_analyzer as qa_mod

    run_mode = "record" if args.record else "replay"
    store = FixtureStore(FIXTURES)
    qa_node = qa_mod.build_query_analyzer_node(
        RecordReplayClient("QueryAnalyzer", qa_mod._SYSTEM, store, run_mode)
    )
    planner_node = planner_mod.build_planner_node(
        RecordReplayClient("Planner", planner_mod._SYSTEM, store, run_mode)
    )

    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        cases = cases[: args.limit]
    started = time.monotonic()
    results = []
    for i, case in enumerate(cases, 1):
        results.append(run_case(case, qa_node, planner_node))
        if args.record and i % 10 == 0:
            print(f"  {i}/{len(cases)} cases ({time.monotonic() - started:.0f}s)", flush=True)
    metrics, failures = score(cases, results)
    report = {
        "sha": _git_sha(),
        "mode": run_mode,
        "cases": len(cases),
        "recorded": sum(1 for r in results if r.get("recorded")),
        "seconds": round(time.monotonic() - started, 1),
        "metrics": metrics,
        "judge_score": None,
        "judge": "not configured (set ARIA_JUDGE_MODEL and add labelled answers)"
        if args.judge
        else "not run",
        "failures": failures,
    }
    out = args.out or RESULTS / f"{report['sha']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    baseline = (
        json.loads(args.compare.read_text(encoding="utf-8"))
        if args.compare and args.compare.exists()
        else None
    )
    if args.markdown:
        args.markdown.write_text(diff_markdown(report, baseline), encoding="utf-8")
    print(diff_markdown(report, baseline))
    print(f"\n{len(failures)} failing checks; results -> {out}")
    if baseline:
        problems = compare(report, baseline)
        if problems:
            print("\nREGRESSION:\n- " + "\n- ".join(problems))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
