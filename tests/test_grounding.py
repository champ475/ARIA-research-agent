"""Grounding: a worked example in a concept answer is not a data claim (live smoke 2026-10-18)."""

from __future__ import annotations

from graph.data_questions import NO_LIVE_DATA_INSTRUCTION
from graph.grounding import check


def test_worked_example_in_a_concept_answer_is_not_flagged():
    """A P/E explanation carried 'Unverified figures: ₹500, 50, ₹200'."""
    prompt = NO_LIVE_DATA_INSTRUCTION + "user: What does a high P/E ratio mean?"
    answer = (
        "A high P/E means investors pay more per rupee of earnings.\n\n"
        "Example:\nIf Company A trades at ₹500 per share and earns ₹10 per share, its P/E is 50."
    )
    assert check(answer, prompt).unsupported == []


def test_the_same_sentence_is_still_checked_when_data_was_fetched():
    assert check("Reliance trades at ₹500 per share, P/E 50.", "Price 1211").unsupported


def test_a_figure_stated_as_current_data_is_still_flagged_in_a_concept_answer():
    prompt = NO_LIVE_DATA_INSTRUCTION + "user: How is TCS?"
    assert check("TCS trades at ₹2,465 today.", prompt).unsupported
