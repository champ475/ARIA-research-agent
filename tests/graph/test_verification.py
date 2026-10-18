import json
from datetime import UTC, datetime

from agents.llm_client import LLMProvidersExhausted
from graph.nodes.verification import build_verification_node


class _StubClient:
    def __init__(self, response: str):
        self._response = response
        self.call_count = 0

    def ask(self, prompt, **kwargs):
        self.call_count += 1
        return self._response


class _ExhaustedClient:
    """Raises LLMProvidersExhausted on every ask() call — simulates every
    configured LLM provider being unavailable."""

    def ask(self, prompt, **kwargs):
        raise LLMProvidersExhausted(Exception("all providers down"), is_byo=False)


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "fetch_info",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "evidence_type": "quote_info",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(UTC),
        "confidence": 0.9,
        "citation_key": "quote_info",
    }
    base.update(overrides)
    return base


def _task(**overrides):
    base = {
        "id": "fetch_info:RELIANCE",
        "description": "x",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "done",
        "result_evidence_ids": ["ev-1"],
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

    def test_dataframe_payload_does_not_crash_missing_field_check(self):
        # Regression: DataFrame.__eq__ returns an elementwise frame, not a
        # bool — `payload in (None, "")` used to raise ValueError the moment
        # any DataFrame-payload evidence (fetch_ohlcv, fetch_nav,
        # fetch_category_funds) reached Verification. See _is_missing().
        import pandas as pd

        stub = _StubClient(json.dumps({"issues": []}))
        ev = _evidence(evidence_type="price_ohlcv", payload=pd.DataFrame({"Close": [1, 2, 3]}))
        node = build_verification_node(stub)
        result = node({"evidence": [ev], "tasks": []})
        assert result["verification_issues"] == []


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
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert stub.call_count == 1
        assert result["verification_issues"] == []

    def test_parses_llm_reported_issues(self):
        stub = _StubClient(
            json.dumps(
                {
                    "issues": [
                        {
                            "claim": "verdict not supported by evidence",
                            "issue_type": "unsupported",
                            "evidence_ids": ["ev-1"],
                        }
                    ],
                }
            )
        )
        node = build_verification_node(stub)
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert result["verification_issues"][0]["stage"] == "llm_judge"
        assert result["verification_issues"][0]["issue_type"] == "unsupported"

    def test_unparseable_output_is_unavailable_not_clean(self):
        # Smoke test 2026-10-17: an empty judge reply used to count as "clean".
        stub = _StubClient("not json")
        node = build_verification_node(stub)
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert result["verification_issues"] == []
        assert result["judge_status"] == "unavailable"
        assert "unavailable" in result["research_journal"][0]["decision"]

    def test_empty_reply_is_unavailable_not_clean(self):
        node = build_verification_node(_StubClient(""))
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert result["judge_status"] == "unavailable"

    def test_a_valid_empty_issue_list_is_clean(self):
        node = build_verification_node(_StubClient(json.dumps({"issues": []})))
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert result["judge_status"] == "ok"
        assert "clean" in result["research_journal"][0]["decision"]

    def test_judge_issues_are_soft_and_earlier_stages_are_hard(self):
        stub = _StubClient(
            json.dumps({"issues": [{"claim": "c", "issue_type": "unsupported", "evidence_ids": []}]})
        )
        soft = build_verification_node(stub)(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert soft["verification_issues"][0]["severity"] == "soft"
        hard = build_verification_node(_StubClient(json.dumps({"issues": []})))(
            {
                "evidence": [_evidence(payload={"pe": float("nan")}, evidence_type="fundamental_ratio")],
                "tasks": [_task()],
                "objective": "x",
                "evidence_summary": {},
            }
        )
        assert hard["verification_issues"][0]["severity"] == "hard"

    def test_empty_objective_falls_back_to_the_query(self):
        class _Spy(_StubClient):
            def ask(self, prompt, **kwargs):
                self.prompt = prompt
                return super().ask(prompt, **kwargs)

        spy = _Spy(json.dumps({"issues": []}))
        build_verification_node(spy)(
            {
                "evidence": [_evidence()],
                "tasks": [_task()],
                "objective": "",
                "query": "what about its debt?",
                "evidence_summary": {},
            }
        )
        assert "Objective: what about its debt?" in spy.prompt

    def test_invalid_issue_type_normalized_to_unsupported(self):
        stub = _StubClient(
            json.dumps(
                {
                    "issues": [{"claim": "x", "issue_type": "made_up_type", "evidence_ids": []}],
                }
            )
        )
        node = build_verification_node(stub)
        result = node(
            {
                "evidence": [_evidence()],
                "tasks": [_task()],
                "objective": "Is TCS cheap right now",
                "evidence_summary": {},
            }
        )
        assert result["verification_issues"][0]["issue_type"] == "unsupported"


class TestVerificationJournal:
    def test_writes_journal_entry(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        result = node({"evidence": [_evidence()], "tasks": [_task()]})
        assert result["research_journal"][0]["node"] == "Verification"


class TestVerificationProviderExhaustion:
    """Bug fix: a total LLM-provider failure at the llm_judge stage must
    never look like "clean, all stages passed" — it returns a synthetic
    issue explaining why, and forces the loop to terminate."""

    def test_returns_without_raising_and_reports_synthetic_issue(self):
        node = build_verification_node(_ExhaustedClient())
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert len(result["verification_issues"]) == 1
        assert result["verification_issues"][0]["stage"] == "llm_judge"
        assert "unavailable" in result["verification_issues"][0]["claim"].lower()

    def test_forces_loop_count_to_max_loops(self):
        node = build_verification_node(_ExhaustedClient())
        result = node(
            {
                "evidence": [_evidence()],
                "tasks": [_task()],
                "objective": "x",
                "evidence_summary": {},
                "loop_count": 0,
                "max_loops": 2,
            }
        )
        assert result["loop_count"] == 2

    def test_deterministic_and_semantic_stages_still_run_first(self):
        # A stage-1 issue must still short-circuit before ever reaching the
        # LLM judge — exhaustion handling only matters once stage 3 is hit.
        node = build_verification_node(_ExhaustedClient())
        broken = _evidence(citation_key="")
        result = node({"evidence": [broken], "tasks": []})
        assert result["verification_issues"][0]["stage"] == "deterministic"


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
        stub = _StubClient(
            json.dumps(
                {
                    "issues": [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}],
                }
            )
        )
        node = build_verification_node(stub)
        result = node(
            {
                "evidence": [_evidence()],
                "tasks": [_task()],
                "objective": "Is TCS cheap right now",
                "evidence_summary": {},
            }
        )
        assert result["loop_count"] == 1

    def test_clean_verification_does_not_increment_loop_count(self):
        stub = _StubClient(json.dumps({"issues": []}))
        node = build_verification_node(stub)
        result = node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": "x", "evidence_summary": {}}
        )
        assert "loop_count" not in result


class TestJudgeRestatingTheQuestion:
    """Live traces 2026-10-18: 14 of 34 judge calls flagged the user's own question as an
    uncited claim. That is not a statement in any answer, so it is not an issue."""

    def _run(self, issues, objective):
        stub = _StubClient(json.dumps({"issues": issues}))
        node = build_verification_node(stub)
        return node(
            {"evidence": [_evidence()], "tasks": [_task()], "objective": objective, "evidence_summary": {}}
        )

    def test_claim_equal_to_the_objective_is_dropped_and_the_turn_is_clean(self):
        result = self._run(
            [{"claim": "Analyse INFY", "issue_type": "missing_citation", "evidence_ids": ["a"]}],
            "Analyse INFY",
        )
        assert result["verification_issues"] == []
        assert result["judge_status"] == "ok"
        assert "loop_count" not in result

    def test_punctuation_and_case_do_not_hide_it(self):
        result = self._run(
            [{"claim": "which stock should i buy", "issue_type": "missing_citation", "evidence_ids": []}],
            "Which stock should I buy?",
        )
        assert result["verification_issues"] == []

    def test_a_real_gap_is_kept(self):
        result = self._run(
            [
                {
                    "claim": "ICICI Bank fundamentals are missing",
                    "issue_type": "unsupported",
                    "evidence_ids": [],
                }
            ],
            "Compare HDFC Bank and ICICI Bank",
        )
        assert [i["claim"] for i in result["verification_issues"]] == ["ICICI Bank fundamentals are missing"]
        assert result["loop_count"] == 1
