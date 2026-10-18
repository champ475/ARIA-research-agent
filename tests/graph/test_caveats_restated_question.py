"""The judge often names the user's own question as the claim it could not confirm
(live traces 2026-10-18). The note says it generally instead of quoting the question back."""

from graph.caveats import GENERIC_NOTE, verification_note


def _state(claim, objective="Analyse INFY"):
    return {
        "objective": objective,
        "query": objective,
        "verification_issues": [{"claim": claim, "issue_type": "missing_citation", "severity": "soft"}],
    }


def test_question_as_claim_becomes_a_general_note():
    note = verification_note(_state("Analyse INFY"))
    assert GENERIC_NOTE in note
    assert "Analyse INFY" not in note


def test_case_and_punctuation_do_not_hide_it():
    assert GENERIC_NOTE in verification_note(_state("which stock should i buy", "Which stock should I buy?"))


def test_a_real_claim_is_still_named():
    note = verification_note(_state("ICICI Bank ratios are missing", "Compare HDFC Bank and ICICI Bank"))
    assert "ICICI Bank ratios are missing" in note
    assert GENERIC_NOTE not in note


def test_both_kinds_together():
    state = _state("Analyse INFY")
    state["verification_issues"].append(
        {"claim": "P/E not backed", "issue_type": "unsupported", "severity": "soft"}
    )
    note = verification_note(state)
    assert "P/E not backed" in note and "Analyse INFY" not in note
