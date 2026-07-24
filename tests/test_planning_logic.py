"""Unit tests for utils/planning_logic.py — extracted from agents/planning_agent.py
in Phase 7.3 of the graph-expansion plan so both the legacy agent and
graph/nodes/communication_layer.py's planning branch share identical logic."""
import json
from unittest.mock import patch

from utils.planning_logic import (
    REQUIRED_FIELDS,
    fallback_question,
    intake_turn,
    sanity_note,
)


class _StubClient:
    def __init__(self, response: str):
        self._response = response

    def ask(self, prompt, **kwargs):
        return self._response

    def chat(self, prompt, **kwargs):
        return self._response


class TestFallbackQuestion:
    def test_lists_every_missing_required_field(self):
        q = fallback_question({})
        for field in REQUIRED_FIELDS:
            assert field != "" # sanity: REQUIRED_FIELDS non-empty
        assert "age" in q
        assert "time horizon" in q

    def test_complete_profile_offers_to_build(self):
        complete = {f: "x" for f in REQUIRED_FIELDS}
        assert "go ahead" in fallback_question(complete)


class TestIntakeTurn:
    def test_extracts_updates_and_reply_from_valid_json(self):
        stub = _StubClient(json.dumps({"updates": {"age": 30}, "minimum_complete": False, "reply": "Tell me more"}))
        updates, complete, reply = intake_turn(stub, "I'm 30", {})
        assert updates == {"age": 30}
        assert complete is False
        assert reply == "Tell me more"

    def test_minimum_complete_computed_from_merged_profile_not_just_updates(self):
        existing = {"capital": 1000000, "horizon_years": 20, "goal": "retirement", "risk_tolerance": "moderate"}
        stub = _StubClient(json.dumps({"updates": {"age": 30}, "minimum_complete": False, "reply": "x"}))
        updates, complete, reply = intake_turn(stub, "I'm 30", existing)
        assert complete is True  # merged profile now has all 5 required fields

    def test_malformed_json_falls_back_gracefully(self):
        stub = _StubClient("not valid json")
        updates, complete, reply = intake_turn(stub, "hello", {})
        assert updates == {}
        assert complete is False
        assert reply  # fallback_question always returns non-empty text

    def test_non_dict_updates_field_is_ignored(self):
        stub = _StubClient(json.dumps({"updates": "not a dict", "minimum_complete": False, "reply": "x"}))
        updates, complete, reply = intake_turn(stub, "hi", {})
        assert updates == {}


class TestSanityNote:
    def test_empty_for_missing_age(self):
        assert sanity_note({}) == ""

    def test_baseline_computed_from_age(self):
        note = sanity_note({"age": 30, "horizon_years": 10})
        assert "70%" in note  # 100 - 30, no horizon bonus (< 15 years)

    def test_horizon_bonus_applied_for_long_horizon(self):
        note = sanity_note({"age": 30, "horizon_years": 20})
        assert "80%" in note  # 100 - 30 + 10 horizon bonus

    def test_clamped_to_20_90_range(self):
        note = sanity_note({"age": 90, "horizon_years": 5})
        assert "20%" in note
