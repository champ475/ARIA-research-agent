"""LLM-12 on a real Postgres: feedback is tied to the answer's turn, one
rating per user per answer, triage finds the question and answer, and
confirmed items become golden-case stubs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from services import chat as chat_svc

pytestmark = pytest.mark.integration

TURN = "3f9a1c2b-0000-4000-8000-000000000001"


def _session_with_answer(db, user_id):
    sid = db.session_create(user_id, "chat", title="t")
    db.chat_save(user_id, sid, "chat", "user", "what is TCS's PE?")
    db.chat_save(
        user_id,
        sid,
        "chat",
        "assistant",
        "TCS trades at a P/E of 99.",
        meta={"turn_id": TURN, "route": "equity"},
    )
    return sid


def test_persisted_answers_carry_their_turn_id_without_the_badge(db, two_users, app_module):
    a, _ = two_users
    sid = db.session_create(a, "chat", title="t")
    with patch("utils.logging_setup.turn_id_var") as tv:
        tv.get.return_value = TURN
        chat_svc.persist_turn(a, sid, "q", "*🔀 Routed to: 📈 Equity*\n\nanswer text", True, route="equity")
    rows = db.chat_load(a, sid)
    assert rows[1]["content"] == "answer text"
    assert rows[1]["meta"] == {"turn_id": TURN, "route": "equity"}


def test_rating_twice_updates_one_row_and_triage_shows_context(db, two_users):
    a, _ = two_users
    sid = _session_with_answer(db, a)
    db.feedback_record(a, TURN, 1, session_id=sid)
    db.feedback_record(a, TURN, -1, session_id=sid, reason="A number is wrong", free_text="PE is ~25")
    assert len(db.feedback_for(a)) == 1
    items = db.feedback_negative_since(datetime.now(UTC) - timedelta(days=1))
    assert len(items) == 1
    item = items[0]
    assert item["reason"] == "A number is wrong" and item["free_text"] == "PE is ~25"
    assert item["question"] == "what is TCS's PE?" and "99" in item["answer"]
    assert db.feedback_rate(datetime.now(UTC) - timedelta(days=1)) == {"rated": 1, "negative": 1}


def test_export_writes_golden_stubs_and_marks_triaged(db, two_users, tmp_path):
    import scripts.feedback_triage as triage

    a, _ = two_users
    sid = _session_with_answer(db, a)
    db.feedback_record(a, TURN, -1, session_id=sid, reason="A number is wrong")
    items = db.feedback_negative_since(datetime.now(UTC) - timedelta(days=1))
    out = tmp_path / "feedback.jsonl"
    assert triage.export(items, [items[0]["id"]], out) == 1
    stub = json.loads(out.read_text(encoding="utf-8"))
    assert stub["query"] == "what is TCS's PE?" and stub["source"]["turn_id"] == TURN
    assert db.feedback_mark_triaged([items[0]["id"]]) == 1
    assert db.feedback_negative_since(datetime.now(UTC) - timedelta(days=1)) == []


def test_like_handler_maps_the_message_to_its_turn(db, two_users, app_module):
    """The Chatbot.like event's message index resolves to that answer's turn."""
    import gradio as gr

    a, _ = two_users
    sid = _session_with_answer(db, a)
    demo = app_module.build_ui()
    like_fn = next(fn.fn for fn in demo.fns.values() if getattr(fn.fn, "__name__", "") == "_on_like")
    data = gr.LikeData(target=None, data={"index": 1, "value": "x", "liked": False})
    box, turn, _note = like_fn(data, a, sid, None)
    assert turn == TURN and box["visible"] is True
    assert db.feedback_for(a)[0]["rating"] == -1
