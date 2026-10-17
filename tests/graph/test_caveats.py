from graph.caveats import verification_note
from graph.nodes.conclusion_builder import conclusion_builder


def _soft(claim, ids=()):
    return {
        "claim": claim,
        "issue_type": "missing_citation",
        "stage": "llm_judge",
        "evidence_ids": list(ids),
        "severity": "soft",
    }


class TestVerificationNote:
    def test_empty_when_nothing_to_say(self):
        assert verification_note({}) == ""
        assert verification_note({"verification_issues": [], "judge_status": "ok"}) == ""

    def test_names_the_unconfirmed_claims(self):
        note = verification_note({"verification_issues": [_soft("ICICI Bank ratios"), _soft("HDFC ROE")]})
        assert "Could not fully confirm: ICICI Bank ratios; HDFC ROE." in note

    def test_caps_the_list(self):
        note = verification_note({"verification_issues": [_soft(f"claim {i}") for i in range(5)]})
        assert "claim 2" in note and "claim 3" not in note and "(and 2 more)" in note

    def test_ignores_talk_about_the_question_itself(self):
        assert verification_note({"verification_issues": [_soft("Objective not specified")]}) == ""

    def test_unavailable_judge_is_said_plainly(self):
        assert "cross-check was unavailable" in verification_note({"judge_status": "unavailable"})

    def test_hard_issues_are_not_repeated_in_the_note(self):
        hard = {
            "claim": "x is NaN",
            "issue_type": "unsupported",
            "stage": "deterministic",
            "evidence_ids": [],
            "severity": "hard",
        }
        assert verification_note({"verification_issues": [hard]}) == ""


def test_soft_flagged_evidence_still_reaches_the_conclusions():
    from datetime import UTC, datetime

    ev = {
        "id": "ev-1",
        "source": "s",
        "tool": "fetch_info",
        "entity": "ICICIBANK",
        "evidence_type": "fundamental_ratio",
        "payload": {"pe": 18.0},
        "timestamp": datetime.now(UTC),
        "confidence": 0.9,
        "citation_key": "k",
    }
    soft = conclusion_builder({"evidence": [ev], "verification_issues": [_soft("c", ["ev-1"])]})
    hard = conclusion_builder(
        {"evidence": [ev], "verification_issues": [{**_soft("c", ["ev-1"]), "severity": "hard"}]}
    )
    assert soft["conclusions"] and not hard["conclusions"]
