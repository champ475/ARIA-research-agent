"""OPS-05 on a real Postgres: `capacity_model.py --from-db` replaces the model's
inputs with what the turns and chat_history tables hold."""

from __future__ import annotations

import copy
import uuid

import pytest

from scripts import capacity_model as cm

pytestmark = pytest.mark.integration


def _turn(db, user, intent, calls, tin, tout, status="ok"):
    db._query(
        "INSERT INTO turns (turn_id, user_id, status, error_id, intent, llm_calls, tokens_in, tokens_out) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), user, status, "e", intent, calls, tin, tout),
        fetch=None,
        commit=True,
    )


def test_measured_inputs_replace_the_defaults_where_there_is_enough_data(db, two_users, pg_url):
    a, b = two_users
    for i in range(6):  # six equity turns: enough
        _turn(db, a if i % 2 else b, "equity", 5, 4000 + i * 100, 900)
    for _ in range(3):  # three mf turns: not enough to trust
        _turn(db, a, "mf", 9, 9999, 9999)
    _turn(db, a, "equity", 99, 1, 1, status="error")  # failed turns say nothing about a normal one
    sid = db.session_create(a, "chat", title="t")
    db.chat_save(a, sid, "chat", "user", "q")
    db.chat_save(a, sid, "chat", "assistant", "a" * 2000)

    measured = cm.measure_from_db(copy.deepcopy(cm.DEFAULTS), pg_url)
    assert measured.calls_per_turn["equity"] == pytest.approx(5.0)
    assert measured.tokens_in["equity"] == pytest.approx(4250)
    assert measured.tokens_out["equity"] == pytest.approx(900)
    assert measured.calls_per_turn["mf"] == cm.DEFAULTS.calls_per_turn["mf"]  # untouched
    assert measured.turns_per_dau_per_day == pytest.approx(5.0)  # user a made 7 turns today, user b made 3
    assert measured.bytes_per_turn != cm.DEFAULTS.bytes_per_turn


def test_an_empty_database_leaves_the_defaults_alone(db, pg_url):
    measured = cm.measure_from_db(copy.deepcopy(cm.DEFAULTS), pg_url)
    assert measured == cm.DEFAULTS
