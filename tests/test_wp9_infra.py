"""WP9 (smoke test 2026-10-17): NaN-safe cache payloads, session retry, who the header says you are,
far-off alert levels, and the phase timings in the turn-end log."""

import math
from types import SimpleNamespace

import pytest

from graph.alert_spec import far_from_price, parse_alert
from utils.db import _json_safe


class TestJsonSafe:
    def test_nan_and_infinity_become_none_everywhere(self):
        out = _json_safe(
            {"a": float("nan"), "b": [1.0, float("-inf")], "c": {"d": float("inf")}, "e": (2, float("nan"))}
        )
        assert out == {"a": None, "b": [1.0, None], "c": {"d": None}, "e": [2, None]}

    def test_ordinary_values_pass_through(self):
        payload = {"n": 3, "f": 1.5, "s": "x", "none": None, "flag": True}
        assert _json_safe(payload) == payload

    def test_the_result_is_strict_json(self):
        import json

        json.dumps(_json_safe({"x": math.nan}), allow_nan=False)


class TestSessionRetry:
    def _request(self):
        return SimpleNamespace(cookies={"aria_session": "tok"})

    def test_a_dropped_connection_is_retried_once(self, monkeypatch):
        from web import session

        calls = []

        def flaky(token):
            calls.append(token)
            if len(calls) == 1:
                raise RuntimeError("connection dropped")
            return "info"

        monkeypatch.setattr(session, "session_info", flaky)
        assert session.current_session(self._request()) == "info"
        assert len(calls) == 2

    def test_two_failures_read_as_logged_out(self, monkeypatch):
        from web import session

        monkeypatch.setattr(
            session, "session_info", lambda token: (_ for _ in ()).throw(RuntimeError("down"))
        )
        assert session.current_session(self._request()) is None

    def test_one_call_when_all_is_well(self, monkeypatch):
        from web import session

        calls = []
        monkeypatch.setattr(session, "session_info", lambda token: calls.append(token) or "info")
        assert session.current_session(self._request()) == "info"
        assert len(calls) == 1


class TestWhoTheHeaderSaysYouAre:
    @pytest.fixture
    def fake_db(self, monkeypatch):
        import utils.db as db

        state = {"admin": False, "byo": None}
        monkeypatch.setattr(db, "user_is_admin", lambda uid: state["admin"])
        monkeypatch.setattr(db, "llm_config_get", lambda uid: state["byo"])
        monkeypatch.setattr(
            db, "_query", lambda *a, **k: {"free_msgs_used_today": 3, "free_msgs_reset_at": None}
        )
        return state

    def test_admin_is_not_told_they_are_on_a_trial(self, fake_db):
        from services import settings

        fake_db["admin"] = True
        assert settings.header_pill(7) == "Admin · no daily message limit"
        assert "Admin account" in settings.llm_status_md(7) and "left today" not in settings.llm_status_md(7)

    def test_a_user_with_a_key_sees_that(self, fake_db):
        from services import settings

        fake_db["byo"] = {"model": "m", "base_url": None}
        assert "own key" in settings.header_pill(7)

    def test_everyone_else_sees_the_trial_text(self, fake_db):
        from services import settings

        assert settings.header_pill(7) == settings.DEFAULT_PILL
        assert settings.header_pill(None) == settings.DEFAULT_PILL

    def test_header_html_escapes_the_pill(self):
        from ui.theme import header_html

        assert "<b>" not in header_html("<b>x</b>") and "&lt;b&gt;" in header_html("<b>x</b>")


class TestFarAlertLevels:
    def test_a_one_rupee_level_for_a_3000_rupee_stock_is_flagged(self):
        spec = parse_alert("alert me when TCS drops below 1 rupee", "TCS")
        assert far_from_price(spec, 3000.0)

    def test_a_sensible_level_is_not(self):
        assert not far_from_price(parse_alert("alert me when TCS drops below 2800", "TCS"), 3000.0)
        assert not far_from_price(parse_alert("alert me when TCS goes above 3300", "TCS"), 3000.0)

    def test_a_level_three_times_higher_is_flagged(self):
        assert far_from_price(parse_alert("alert me if TCS goes above 12000", "TCS"), 3000.0)

    def test_unknown_price_is_never_flagged(self):
        assert not far_from_price(parse_alert("alert me when TCS drops below 1", "TCS"), None)


def test_turn_end_log_names_each_phase():
    from ui.chat import _phase_ms

    marks = {"start": 0.0, "orchestrator": 1.5, "answer": 12.0, "suggestions": 12.2, "end": 12.3}
    assert _phase_ms(marks) == " orchestrator_ms=1500 answer_ms=10500 suggestions_ms=200 persist_ms=100"
    assert _phase_ms({"start": 0.0, "end": 2.0}) == " persist_ms=2000"


def test_pool_settings_are_typed_and_sane():
    import config

    assert config.DB_POOL_ACQUIRE_TIMEOUT >= config.DB_CONNECT_TIMEOUT
    assert 0 < config.DB_POOL_MAX_IDLE < 300  # shorter than Neon's idle drop
