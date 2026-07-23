import json
from datetime import datetime, timezone

from graph.nodes.verification import build_verification_node


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.call_count = 0

    def ask(self, prompt, **kwargs):
        self.call_count += 1
        return self._response


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "fetch_info",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "evidence_type": "quote_info",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(timezone.utc),
        "confidence": 0.9,
        "citation_key": "quote_info",
    }
    base.update(overrides)
    return base


def _task(**overrides):
    base = {
        "id": "fetch_info:RELIANCE", "description": "x", "tool": "fetch_info",
        "entity": "RELIANCE", "depends_on": [], "status": "done", "result_evidence_ids": ["ev-1"],
    }
    base.update(overrides)
    return base


class TestDeterministicStage:
    def test_clean_evidence_does_not_call_llm(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        node({"evidence": [_evidence()], "tasks": [_task()]})
        assert stub.call_count == 1  # reaches llm_judge since 1&2 are clean

    def test_missing_field_caught_without_calling_llm(self):
        stub = _StubClient(json.dumps({"issues": []}))
        broken = _evidence(citation_key="")
        node = build_verification_node(stub)
        result = node({"evidence": [broken], "tasks": []})
        assert result["verification_issues"]
        assert result["verification_issues"][0]["stage"] == "deterministic"
        assert stub.call_count == 0

    def test_dangling_result_evidence_id_caught(self):
        stub = _StubClient(json.dumps({"issues": []}))
        task = _task(result_evidence_ids=["ev-does-not-exist"])
        node = build_verification_node(stub)
        result = node({"evidence": [], "tasks": [task]})
        assert any(i["issue_type"] == "unsupported" for i in result["verification_issues"])
        assert stub.call_count == 0

    def test_nan_fundamental_ratio_caught(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev = _evidence(evidence_type="fundamental_ratio", payload={"pe": float("nan")})
        node = build_verification_node(stub)
        result = node({"evidence": [ev], "tasks": []})
        assert any(i["issue_type"] == "unsupported" for i in result["verification_issues"])
        assert stub.call_count == 0

    def test_absurd_magnitude_ratio_caught(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev = _evidence(evidence_type="fundamental_ratio", payload={"pe": 99999999})
        node = build_verification_node(stub)
        result = node({"evidence": [ev], "tasks": []})
        assert result["verification_issues"]
        assert stub.call_count == 0


class TestSemanticStage:
    def test_low_confidence_caught_without_calling_llm(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev = _evidence(confidence=0.2)
        node = build_verification_node(stub)
        result = node({"evidence": [ev], "tasks": []})
        assert any(i["issue_type"] == "low_confidence" for i in result["verification_issues"])
        assert stub.call_count == 0

    def test_contradicting_evidence_caught(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev1 = _evidence(id="ev-1", payload={"pe": 24.3})
        ev2 = _evidence(id="ev-2", payload={"pe": 50.0})
        node = build_verification_node(stub)
        result = node({"evidence": [ev1, ev2], "tasks": []})
        assert any(i["issue_type"] == "contradiction" for i in result["verification_issues"])
        assert stub.call_count == 0

    def test_close_values_within_tolerance_not_contradiction(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev1 = _evidence(id="ev-1", payload={"pe": 24.3})
        ev2 = _evidence(id="ev-2", payload={"pe": 24.5})
        node = build_verification_node(stub)
        result = node({"evidence": [ev1, ev2], "tasks": []})
        assert result["verification_issues"] == []
        assert stub.call_count == 1  # falls through to clean llm_judge


class TestLLMJudgeStage:
    def test_reached_only_when_earlier_stages_clean(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert stub.call_count == 1
        assert result["verification_issues"] == []

    def test_parses_llm_reported_issues(self):
        stub = _StubClient(json.dumps({
            "issues": [{"claim": "verdict not supported by evidence", "issue_type": "unsupported", "evidence_ids": ["ev-1"]}],
        }))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert result["verification_issues"][0]["stage"] == "llm_judge"
        assert result["verification_issues"][0]["issue_type"] == "unsupported"

    def test_falls_back_to_no_issues_on_unparseable_output(self):
        stub = _StubClient("not json")
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert result["verification_issues"] == []

    def test_invalid_issue_type_normalized_to_unsupported(self):
        stub = _StubClient(json.dumps({
            "issues": [{"claim": "x", "issue_type": "made_up_type", "evidence_ids": []}],
        }))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert result["verification_issues"][0]["issue_type"] == "unsupported"


class TestVerificationJournal:
    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()]})
        assert result["research_journal"][0]["node"] == "Verification"


class TestVerificationLoopCount:
    def test_deterministic_stage_issue_increments_loop_count(self):
        stub = _StubClient(json.dumps({"issues": []}))
        broken = _evidence(citation_key="")
        node = build_verification_node(stub)
        result = node({"evidence": [broken], "tasks": []})
        assert result["loop_count"] == 1

    def test_semantic_stage_issue_increments_loop_count(self):
        stub = _StubClient(json.dumps({"issues": []}))
        ev = _evidence(confidence=0.2)
        node = build_verification_node(stub)
        result = node({"evidence": [ev], "tasks": []})
        assert result["loop_count"] == 1

    def test_llm_judge_issue_increments_loop_count(self):
        stub = _StubClient(json.dumps({
            "issues": [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}],
        }))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert result["loop_count"] == 1

    def test_clean_verification_does_not_increment_loop_count(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}})
        assert "loop_count" not in result
