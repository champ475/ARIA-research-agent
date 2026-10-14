"""Shared fixtures for the graph test suite."""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest


def import_app_without_side_effects():
    """Import app.py without its import-time side effects: init_db() against
    whatever DATABASE_URL .env holds, and the Telegram polling thread. Unit
    tests must never touch a real database."""
    if "app" in sys.modules:
        return sys.modules["app"]
    with patch("utils.db.init_db"), patch("utils.telegram_link.start_polling_thread"):
        import app
    return app


@pytest.fixture(autouse=True)
def _no_real_database(request, monkeypatch):
    """Unit tests must never open a Postgres connection: locally .env's
    DATABASE_URL is the production database, and in CI there is none. Only
    tests under tests/integration/ (which point utils.db at a throwaway
    database) may connect. psycopg2's pools connect via psycopg2.connect, so
    this also covers utils.db.get_conn()."""
    if "integration" in request.node.nodeid.split("::")[0].replace("\\", "/").split("/"):
        return

    def _refuse(*args, **kwargs):
        raise RuntimeError(
            f"{request.node.nodeid} tried to open a real database connection — "
            "mock the utils.db call, or move the test to tests/integration/"
        )

    monkeypatch.setattr("psycopg2.connect", _refuse)


@pytest.fixture(autouse=True)
def _no_local_llm(monkeypatch):
    """A developer's .env may set LOCAL_LLM_BASE_URL; unit tests must never
    reach a real local model server. Tests that need it re-enable it."""
    monkeypatch.setattr("agents.providers.local_llm.LOCAL_LLM_BASE_URL", "")


@pytest.fixture(scope="session")
def app_module():
    return import_app_without_side_effects()


def _db_available() -> bool:
    try:
        from config import DATABASE_URL
    except ImportError:
        return False
    return bool(DATABASE_URL) and os.getenv("RUN_DB_TESTS") == "1"


requires_db = pytest.mark.skipif(
    not _db_available(),
    reason="DATABASE_URL not configured or RUN_DB_TESTS!=1 — skipping DB-touching test",
)


@pytest.fixture
def sample_evidence() -> dict:
    return {
        "id": "reliance-fundamental-pe-1",
        "source": "yfinance",
        "tool": "tools.analysis.fundamental.compute",
        "entity": "RELIANCE",
        "evidence_type": "fundamental_ratio",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(UTC),
        "confidence": 0.9,
        "citation_key": "pe",
    }


@pytest.fixture
def sample_task() -> dict:
    return {
        "id": "fetch-fundamentals-reliance",
        "description": "Fetch fundamentals for RELIANCE",
        "tool": "tools.analysis.fundamental.compute",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
