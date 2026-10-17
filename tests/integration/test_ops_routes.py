"""OPS-04 on a real Postgres: /healthz and /readyz."""

from __future__ import annotations

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db, monkeypatch):
    import config
    from server import create_app
    from utils import lifecycle

    lifecycle.reset()
    monkeypatch.setattr(config, "GROQ_API_KEYS", ["gsk_test"])
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c
    lifecycle.reset()


def test_healthz_is_unauthenticated_and_up(client):
    r = client.get("/healthz", follow_redirects=False)
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz_ready_when_db_at_head_and_a_provider_is_configured(client):
    r = client.get("/readyz")
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "ready"


def test_readyz_503_while_draining(client):
    from utils import lifecycle

    lifecycle.begin_drain()
    r = client.get("/readyz")
    assert r.status_code == 503 and "lifecycle" in r.json()["problems"]


def test_readyz_503_when_database_unreachable(client, monkeypatch):
    from utils import db

    def down():
        raise db.OperationalError("connection refused to 10.0.0.9")

    monkeypatch.setattr(db, "get_conn", down)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["problems"]["database"] == "database unreachable"
    assert "10.0.0.9" not in r.text


def test_readyz_503_when_schema_behind(client, monkeypatch):
    from utils import migrations

    def behind(conn, require_checkpointer=True):
        raise migrations.SchemaOutOfDate("at 0006, needs 0007")

    monkeypatch.setattr(migrations, "check_schema", behind)
    r = client.get("/readyz")
    assert r.status_code == 503 and "schema" in r.json()["problems"]["database"]


def test_readyz_503_without_any_llm_provider(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "GROQ_API_KEYS", [])
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "")
    r = client.get("/readyz")
    assert r.status_code == 503 and "llm" in r.json()["problems"]
