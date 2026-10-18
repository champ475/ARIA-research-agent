"""
evals/answers/run.py — the answer-level eval (EVAL-01).

The golden set (evals/runner.py) stops at the Planner. This suite runs each
case's turns through the whole product path — guardrails, the real LangGraph
research graph, the streamed answer, grounding, finalize, notes — inside one
GraphOrchestrator per case (so follow-ups use its real memory), and scores the
text a user would read against reference facts (evals/answers/score.py).

Deterministic and offline in replay mode:
- LLM replies come from evals/fixtures/answers.jsonl (evals/answers/replay.py);
- market data from tests/fixtures (evals/answers/fixture_provider.py), HTTP
  sources from data/fake (ARIA_FAKE_DATA);
- the clock is pinned to FAKE_AS_OF and evidence ids are content-derived;
- no database: every connection is refused, the evidence cache is off.

    python evals/answers/run.py                       # replay, report
    python evals/answers/run.py --compare evals/answers/baseline.json
    python evals/answers/run.py --record --family tax # fill missing replies (real LLM)
    python evals/answers/run.py --case eq-itc-div -v  # one case, print the answers

Recording reads only the LLM keys from .env, never its DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.answers.memdb import TARGETS, Router  # noqa: E402  (no app imports: safe before config)

CASES = ROOT / "evals" / "answers" / "cases.jsonl"
FIXTURES = ROOT / "evals" / "fixtures" / "answers.jsonl"
RESULTS = ROOT / "evals" / "results"
EVAL_USER_ID = 999_001
MEMDB = Router()

_LLM_ENV = ("GROQ_API_KEY", "GEMINI_API_KEY", "ARIA_MODEL_", "LLM_", "GROQ_", "GEMINI_")


def _isolate_environment(record: bool) -> None:
    """Before config is imported: no .env (so no production DATABASE_URL), no
    tracing, fake HTTP sources; when recording, the LLM keys from .env only."""
    os.environ["ARIA_NO_DOTENV"] = "1"
    os.environ["ARIA_FAKE_DATA"] = "1"
    os.environ["ARIA_FAKE_LLM"] = "0"
    os.environ["ARIA_TRACING"] = ""
    os.environ["DATABASE_URL"] = ""
    os.environ["LOCAL_LLM_BASE_URL"] = ""
    os.environ.setdefault("SECRET_KEY", "eval-only-not-a-secret-0123456789abcdef")
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[var] = "false"
    if record:
        from dotenv import dotenv_values

        for k, v in dotenv_values(ROOT / ".env").items():
            if v and k.startswith(_LLM_ENV):
                os.environ.setdefault(k, v)


def _patch_offline(store) -> list:
    """Every seam the eval pins. Returns the started patchers."""
    from unittest.mock import patch

    from agents import llm_client
    from evals.answers.fixture_provider import FixtureProvider
    from evals.answers.replay import ReplayProvider
    from graph.nodes import execute_task
    from tools.data import providers
    from tools.data.providers.fake import FAKE_AS_OF

    def refuse(*args, **kwargs):
        raise RuntimeError("the answer eval has no database")

    from utils import market_clock

    real_now = market_clock._now
    provider = FixtureProvider()
    providers.set_provider(provider)
    llm_client.set_provider_override(lambda system, node: ReplayProvider(system, node, store))
    execute_task.REPLAYABLE = True
    execute_task.PINNED_NOW = FAKE_AS_OF
    patchers = [
        patch("psycopg.connect", refuse),
        patch("psycopg.Connection.connect", refuse),
        patch("utils.db._get_pool", refuse),
        patch("utils.tracing._persist", lambda *a, **k: None),
        patch("utils.db.audit_log_write", lambda *a, **k: None),
        patch("tools.data.evidence_cache.get", lambda *a, **k: None),
        patch("tools.data.evidence_cache.put", lambda *a, **k: None),
        patch("graph.tool_catalogue._current_price", lambda symbol: None),
        patch("utils.market_clock._now", lambda now=None: real_now(now or FAKE_AS_OF)),
        patch("tools.screener.get_info", provider.quote),
        patch("tools.screener.snapshot_status", lambda: {"rows": 0, "oldest": None, "newest": None}),
        patch("agents.providers.local_llm.LOCAL_LLM_BASE_URL", ""),
    ]
    patchers += [patch(target, MEMDB.method(name)) for target, name in TARGETS.items()]
    for p in patchers:
        p.start()
    return patchers


def _orchestrator_class():
    from agents.graph_orchestrator import GraphOrchestrator

    class EvalOrchestrator(GraphOrchestrator):
        """The real orchestrator, minus the database: trial path, never limited,
        and it keeps each turn's final graph state and turn record."""

        last_state: dict | None = None
        last_rec = None

        def _resolve_byo_config(self):
            return None

        def _trial_quota_exceeded(self) -> bool:
            return False

        def _run_graph(self, payload, config):
            result = yield from super()._run_graph(payload, config)
            self.last_state = result
            return result

        def _process_stream(self, message, rec):
            self.last_state, self.last_rec = None, rec
            yield from super()._process_stream(message, rec)

    return EvalOrchestrator


def _turn_record(orch, text: str, misses_before: int, store) -> dict:
    state = orch.last_state or {}
    rec = orch.last_rec
    return {
        "text": text,
        "intent": state.get("intent"),
        "advice_mode": state.get("advice_mode"),
        "entities": state.get("entities") or [],
        "tools": [
            {"tool": t.get("tool"), "entity": t.get("entity"), "status": t.get("status")}
            for t in state.get("tasks") or []
        ],
        "evidence": [
            {"tool": e.get("tool"), "entity": e.get("entity"), "type": e.get("evidence_type")}
            for e in state.get("evidence") or []
        ],
        "conclusions": [c.get("claim") for c in state.get("conclusions") or []],
        "verification_issues": state.get("verification_issues") or [],
        "judge_status": state.get("judge_status"),
        "llm_calls": rec.llm_calls if rec else 0,
        "grounding": rec.grounding if rec else None,
        "status": rec.status if rec else None,
        "misses": store.misses[misses_before:],
    }


def run_case(case: dict, store) -> dict:
    MEMDB.reset()  # each case starts with no alerts, reports or planning profile
    orch = _orchestrator_class()(user_id=EVAL_USER_ID)
    turns = []
    for turn in case["turns"]:
        before = len(store.misses)
        try:
            text = orch.process(turn["q"])[0] or ""
        except Exception as e:  # a crash is a failed case, not a failed run
            logging.getLogger(__name__).warning("case %s crashed", case["id"], exc_info=True)
            text = f"[crashed: {type(e).__name__}]"
        turns.append(_turn_record(orch, text, before, store))
    return {"id": case["id"], "family": case["family"], "turns": turns}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ARIA answer-level eval (EVAL-01)")
    ap.add_argument(
        "--record", action="store_true", help="call the real LLM for missing replies and save them"
    )
    ap.add_argument("--family", action="append", help="only this family (repeatable)")
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    ap.add_argument("--limit", type=int, help="only the first N selected cases")
    ap.add_argument("--compare", type=Path, help="baseline JSON; exit 1 on a regression or a gate breach")
    ap.add_argument("--enforce", action="store_true", help="exit 1 when an absolute gate fails (EVAL-01b)")
    ap.add_argument("--out", type=Path, help="results file (default evals/results/answers-<sha>.json)")
    ap.add_argument("--markdown", type=Path, help="also write a Markdown summary (PR comment)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every answer")
    args = ap.parse_args(argv)

    _isolate_environment(args.record)
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

    from evals.answers import score
    from evals.answers.replay import ReplayStore
    from evals.runner import _git_sha

    store = ReplayStore(FIXTURES, "record" if args.record else "replay")
    patchers = _patch_offline(store)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.family:
        cases = [c for c in cases if c["family"] in args.family]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.limit:
        cases = cases[: args.limit]

    started = time.monotonic()
    results = []
    try:
        for i, case in enumerate(cases, 1):
            result = run_case(case, store)
            results.append(result)
            if args.verbose:
                for t, turn in zip(case["turns"], result["turns"], strict=True):
                    print(f"\n=== {case['id']} :: {t['q']}\n{turn['text']}\n--- tools={turn['tools']}")
            if args.record:
                print(f"  {i}/{len(cases)} {case['id']} ({time.monotonic() - started:.0f}s)", flush=True)
    finally:
        for p in patchers:
            p.stop()

    report = score.report(cases, results)
    report.update(
        sha=_git_sha(),
        mode=store.mode,
        seconds=round(time.monotonic() - started, 1),
    )
    out = args.out or RESULTS / f"answers-{report['sha']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({**report, "results": results}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    baseline = (
        json.loads(args.compare.read_text(encoding="utf-8"))
        if args.compare and args.compare.exists()
        else None
    )
    md = score.markdown(report, baseline)
    if args.markdown:
        args.markdown.write_text(md, encoding="utf-8")
    print(md)
    print(f"\nresults -> {out}")
    problems = score.gate_problems(report) if args.enforce else []
    if baseline:
        problems += score.regressions(report, baseline)
    if problems:
        print("\nFAILED:\n- " + "\n- ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
