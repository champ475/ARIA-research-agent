"""COMP-03 on a real Postgres: "download my data" covers every user-scoped
table (discovered from the schema), withholds secrets, and contains nobody
else's rows."""

from __future__ import annotations

import json
import re

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db):
    from server import create_app
    from utils import auth

    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c


def _form(client, page, **data):
    html = client.get(page).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(page, data={**data, "csrf": csrf}, follow_redirects=False)


def test_export_contains_every_user_table_and_only_the_users_rows(client, db, two_users):
    _, bystander = two_users
    _form(client, "/signup", email="jo@example.com", password="export-pass-1", agree="yes")
    _form(client, "/login", email="jo@example.com", password="export-pass-1")
    uid = db.user_by_email("jo@example.com")["id"]
    db.watchlist_add(uid, "TCS")
    db.alert_add(uid, "TCS", "price_below", "below 3500", 3500.0)
    sid = db.session_create(uid, "chat", title="my chat")
    db.chat_save(uid, sid, "chat", "user", "what is TCS trading at")
    db.profile_upsert(uid, {"age": 41})
    db.llm_config_set(uid, "openai_compatible", "gpt-x", "gAAAAAsecret-ciphertext-value-123456")
    db.watchlist_add(bystander, "INFY")
    db.chat_save(
        bystander, db.session_create(bystander, "chat", title="theirs"), "chat", "user", "bystander secret"
    )

    r = client.get("/account/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = json.loads(r.content)

    assert set(db.user_scoped_tables()) <= set(data["tables"])  # every table, from the live schema
    assert data["account"]["email"] == "jo@example.com"
    assert data["tables"]["watchlist"][0]["symbol"] == "TCS"
    assert data["tables"]["chat_history"][0]["content"] == "what is TCS trading at"
    assert data["tables"]["financial_profiles"]
    for rows in data["tables"].values():
        for row in rows:
            assert row.get("user_id") in (None, uid)

    body = r.text
    assert "bystander secret" not in body and "INFY" not in body
    assert "secret-ciphertext" not in body
    assert data["account"]["password_hash"] == "[withheld: security secret]"
    assert all(s["token_hash"] == "[withheld: security secret]" for s in data["tables"]["sessions"])
    assert db.audit_log_for(uid)[0]["action"] == "account.export"


def test_export_requires_login(client):
    r = client.get("/account/export", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_user_rows_refuses_tables_outside_the_schema_list(db):
    with pytest.raises(ValueError):
        db.user_rows("users; DROP TABLE users", 1)
