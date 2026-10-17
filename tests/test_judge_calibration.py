"""LLM-13: the labelled set is well-formed, the scorer is right, and the
recorded judge replies still meet the precision / recall / gate-rate targets."""

from __future__ import annotations

import json

import pytest

from evals import judge_calibration as jc
from evals.runner import FixtureStore, RecordReplayClient
from graph.nodes import verification


def test_label_set_shape():
    labels = jc.load_labels()
    assert len(labels) == 100
    assert len({c["id"] for c in labels}) == 100
    assert sum(c["label"] == "sufficient" for c in labels) == 50
    assert sum(c["label"] == "insufficient" for c in labels) == 50
    assert {c["family"] for c in labels if c["label"] == "insufficient"} == {
        "wrong_entity", "wrong_type", "missing_leg", "empty", "low_signal",
    }  # fmt: skip


def test_label_file_is_up_to_date():
    from evals import build_judge_labels as b

    built = "\n".join(json.dumps(c, ensure_ascii=False) for c in b.cases) + "\n"
    assert jc.LABELS.read_text(encoding="utf-8").replace("\r\n", "\n") == built


def test_score_math():
    rows = (
        [{"label": "insufficient", "family": "empty", "flagged": True}] * 3
        + [{"label": "insufficient", "family": "empty", "flagged": False}]
        + [{"label": "sufficient", "family": "price", "flagged": True}]
        + [{"label": "sufficient", "family": "price", "flagged": False}] * 4
    )
    m = jc.score(rows)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (3, 1, 1, 4)
    assert m["precision"] == 0.75 and m["recall"] == 0.75 and m["gate_rate"] == 0.2
    assert m["recall_by_family"] == {"empty": "3/4"}


def test_meets_targets_reports_each_miss():
    assert jc.meets_targets({"precision": 0.9, "recall": 0.8, "gate_rate": 0.01}) == []
    assert len(jc.meets_targets({"precision": 0.5, "recall": 0.5, "gate_rate": 0.5})) == 3
    assert jc.meets_targets({"precision": None, "recall": None, "gate_rate": None})


def test_recorded_judge_meets_targets():
    """Fails when the judge prompt or evidence shape changes without re-recording
    (unrecorded cases) or when the judge gets worse."""
    client = RecordReplayClient("verification", verification._SYSTEM, FixtureStore(jc.FIXTURES), "replay")
    rows, unrecorded = jc.run(jc.load_labels(), client)
    assert unrecorded == 0, "re-record: python evals/judge_calibration.py --record"
    assert jc.meets_targets(jc.score(rows)) == []


class _Stub:
    def __init__(self, tag):
        self.tag = tag

    def ask(self, prompt, **kw):
        return json.dumps({"issues": []})


@pytest.mark.parametrize(
    "strong,complexity,expected",
    [(True, "complex", "strong"), (True, "moderate", "fast"), (False, "complex", "fast")],
)
def test_strong_judge_only_for_complex_turns(monkeypatch, strong, complexity, expected):
    built = []

    def fake_llm(system, cfg=None, node=None):
        built.append(node)
        return _Stub(node)

    monkeypatch.setattr(verification, "LLMClient", fake_llm)
    monkeypatch.setattr(verification, "VERIFICATION_STRONG_JUDGE", strong)
    seen = []
    orig = verification.run_judge_checked
    monkeypatch.setattr(
        verification, "run_judge_checked", lambda c, o, e: seen.append(c.tag) or orig(c, o, e)
    )
    node = verification.build_verification_node(None, None)
    node({"evidence": [], "tasks": [], "objective": "x", "evidence_summary": {}, "complexity": complexity})
    assert seen == ["verification_complex" if expected == "strong" else "verification"]


def test_gate_counter_exists():
    from utils import metrics

    metrics.observe_gate("verification_gate")
    assert b'aria_gates_total{gate="verification_gate"}' in metrics.render()
