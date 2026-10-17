"""VAL-05: the planning intake. tests/data/intake_cases.json holds 40
utterances, each with the updates an LLM proposed for it (adversarial ones
included: "-5", "all of it", 500 years, prompt injection, unknown fields)
and what may be saved. No invalid value may reach the database, and the
build trigger may have no false positives on the set."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.financial_profile import (
    FinancialProfile,
    is_complete,
    profile_summary,
    sanitize_profile,
    validate_updates,
    wants_build,
)
from utils.planning_logic import intake_turn

CASES = json.loads((Path(__file__).parent / "data" / "intake_cases.json").read_text(encoding="utf-8"))


class _StubLLM:
    def __init__(self, updates):
        self.updates = updates

    def ask(self, prompt, **kwargs):
        return json.dumps({"updates": self.updates, "minimum_complete": True, "reply": "Noted."})


def test_table_has_40_utterances_with_adversarial_ones():
    assert len(CASES) == 40
    assert sum(1 for c in CASES if c["llm"] and not c["saved"]) >= 15


@pytest.mark.parametrize("case", CASES, ids=[c["say"][:40] for c in CASES])
def test_only_valid_values_are_returned_for_saving(case):
    clean, _complete, reply = intake_turn(_StubLLM(case["llm"]), case["say"], {})
    assert clean == pytest.approx(case["saved"]) if case["saved"] else clean == {}
    FinancialProfile.model_validate(clean)  # whatever is returned is itself valid
    rejected = set(case["llm"]) & set(FinancialProfile.model_fields) - set(case["saved"])
    if rejected:
        assert "I didn't save" in reply  # asked about, not silently dropped


@pytest.mark.parametrize("case", CASES, ids=[c["say"][:40] for c in CASES])
def test_build_trigger_has_no_false_positives(case):
    assert wants_build(case["say"]) is case["build"]


def test_llms_minimum_complete_claim_is_not_trusted():
    llm = _StubLLM(
        {"age": -5, "capital": "lots", "horizon_years": 500, "goal": "x y", "risk_tolerance": "yolo"}
    )
    clean, complete, _ = intake_turn(llm, "here you go", {})
    assert complete is False and clean == {"goal": "x y"}


def test_nothing_invalid_reaches_the_db_through_the_planning_node():
    from graph.nodes.communication_layer import build_communication_layer_node

    saved: list[dict] = []

    def fake_upsert(user_id, updates):
        saved.append(updates)
        return updates

    for case in CASES:
        with (
            patch("graph.nodes.planning_node.profile_get", return_value={}),
            patch("graph.nodes.planning_node.profile_upsert", side_effect=fake_upsert),
            patch("graph.nodes.planning_node._planning_build_strategy", return_value="plan"),
        ):
            node = build_communication_layer_node(_StubLLM(case["llm"]))
            node({"intent": "planning", "query": case["say"], "user_id": 1, "conclusions": []})
    for updates in saved:
        FinancialProfile.model_validate(updates)
        assert validate_updates(updates)[1] == {}


def test_legacy_invalid_stored_values_are_dropped_before_planning():
    legacy = {
        "age": "-5",
        "capital": "lots",
        "horizon_years": 20,
        "goal": "retire",
        "risk_tolerance": "moderate",
    }
    clean = sanitize_profile(legacy)
    assert clean == {"horizon_years": 20, "goal": "retire", "risk_tolerance": "moderate"}
    assert not is_complete(clean)


def test_profile_summary_shows_rupees_and_years():
    text = profile_summary({"age": 30, "capital": 500000.0, "horizon_years": 15, "goal": "house"})
    assert "| Investable amount | ₹5,00,000.00 |" in text and "| Time horizon | 15 years |" in text


def test_a_profile_waiting_for_build_offers_a_build_button():
    from unittest.mock import MagicMock

    from agents.graph_orchestrator import GraphOrchestrator

    orch = GraphOrchestrator.__new__(GraphOrchestrator)
    orch._provider_config = None
    with patch("agents.llm_client.LLMClient", MagicMock(side_effect=AssertionError("no LLM call"))):
        chips = orch.generate_suggestions("... When this looks right, say **build my plan** ...", "I'm 30")
    assert chips[0] == "Build my plan"
