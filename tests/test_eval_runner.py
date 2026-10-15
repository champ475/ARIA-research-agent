"""LLM-01: the golden-set harness itself — scoring, the regression gate,
record/replay, and the integrity of the golden set."""

import json
import time

import pytest

from evals import runner

CASES = [json.loads(line) for line in runner.CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_golden_set_is_large_unique_and_covers_every_domain():
    assert len(CASES) >= 200
    assert len({c["id"] for c in CASES}) == len(CASES)
    domains = {c["domain"] for c in CASES}
    assert {
        "equity",
        "report",
        "mf",
        "screener",
        "alert",
        "planning",
        "chat",
        "val",
        "hinglish",
        "redteam",
    } <= domains
    assert sum(c["domain"] == "redteam" for c in CASES) >= 30
    assert sum(c["domain"] == "hinglish" for c in CASES) >= 20


def test_golden_set_only_names_real_tools_and_listed_symbols():
    from graph.tool_catalogue import list_tools
    from tools.data.instruments import master

    tools = {t.name for t in list_tools()}
    listed = master().by_symbol
    for case in CASES:
        exp = case["expect"]
        assert set(exp.get("tools_any", [])) <= tools, case["id"]
        assert set(exp.get("tools_forbidden", [])) <= tools, case["id"]
        assert all(sym in listed for sym in exp.get("entities") or []), case["id"]


def test_scoring_and_entity_f1():
    cases = [
        {
            "id": "a",
            "query": "q",
            "expect": {
                "guard": "allow",
                "intent": "equity",
                "entities": ["TCS"],
                "blocking": False,
                "min_complexity": "moderate",
                "tools_any": ["fetch_info"],
                "tools_forbidden": ["alert_create"],
            },
        },
        {"id": "b", "query": "q", "expect": {"guard": "refuse", "blocking": False, "tools_forbidden": []}},
    ]
    results = [
        {
            "id": "a",
            "guard": "allow",
            "entities_det": ["TCS"],
            "recorded": True,
            "intent": "mf",
            "entities": ["TCS", "INFY"],
            "complexity": "moderate",
            "blocking": False,
            "tools": ["fetch_info"],
            "executed_forbidden": [],
        },
        {"id": "b", "guard": "refuse", "entities_det": [], "recorded": True, "tools": []},
    ]
    metrics, failures = runner.score(cases, results)
    assert metrics["guard_accuracy"] == 100.0 and metrics["intent_accuracy"] == 0.0
    assert metrics["tool_recall"] == 100.0 and metrics["write_safety"] == 100.0
    assert metrics["entity_f1"] == pytest.approx(66.67, abs=0.01)  # 1 TP, 1 FP
    assert {f["metric"] for f in failures} == {"intent_accuracy", "entity_f1"}


def test_regression_gate():
    base = {"metrics": {"tool_recall": 90.0, "guard_accuracy": 100.0}, "judge_score": 4.0}
    assert (
        runner.compare({"metrics": {"tool_recall": 88.5, "guard_accuracy": 100.0}, "judge_score": 3.9}, base)
        == []
    )
    problems = runner.compare(
        {"metrics": {"tool_recall": 87.0, "guard_accuracy": 100.0}, "judge_score": 3.7}, base
    )
    assert len(problems) == 2 and problems[0].startswith("tool_recall")


def test_record_then_replay(tmp_path, monkeypatch):
    store = runner.FixtureStore(tmp_path / "llm.jsonl")
    replay = runner.RecordReplayClient("Node", "sys", store, "replay")
    with pytest.raises(runner.MissingFixture):
        replay.ask("prompt")

    class _Live:
        def ask(self, prompt, **kwargs):
            return "live answer"

    recorder = runner.RecordReplayClient("Node", "sys", store, "record")
    recorder._live = _Live()
    assert recorder.ask("prompt") == "live answer"
    reloaded = runner.FixtureStore(tmp_path / "llm.jsonl")
    assert runner.RecordReplayClient("Node", "sys", reloaded, "replay").ask("prompt") == "live answer"
    # A different node or system prompt is a different key.
    with pytest.raises(runner.MissingFixture):
        runner.RecordReplayClient("Other", "sys", reloaded, "replay").ask("prompt")


def test_full_replay_is_fast_and_deterministic_metrics_hold(tmp_path):
    started = time.monotonic()
    code = runner.main(["--replay", "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert code == 0 and report["cases"] == len(CASES)
    assert time.monotonic() - started < 120  # PRD: make eval < 10 min
    assert report["metrics"]["guard_accuracy"] == 100.0
    assert report["metrics"]["entity_f1"] == 100.0
