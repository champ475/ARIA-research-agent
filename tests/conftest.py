"""Shared fixtures for the graph test suite."""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest


def import_app_without_side_effects():
    """Import app.py without its import-time side effects: the schema check
    against whatever DATABASE_URL .env holds, and the Telegram polling
    thread. Unit tests must never touch a real database."""
    if "app" in sys.modules:
        return sys.modules["app"]
    with patch("utils.db.ensure_schema"), patch("utils.telegram_link.start_polling_thread"):
        import app
    return app


@pytest.fixture(autouse=True)
def _no_real_database(request, monkeypatch):
    """Unit tests must never open a Postgres connection: locally .env's
    DATABASE_URL is the production database, and in CI there is none. Only
    tests under tests/integration/ (which point utils.db at a throwaway
    database) may connect. psycopg_pool opens connections through
    psycopg.Connection.connect, so this also covers utils.db.get_conn()."""
    if "integration" in request.node.nodeid.split("::")[0].replace("\\", "/").split("/"):
        return

    def _refuse(*args, **kwargs):
        raise RuntimeError(
            f"{request.node.nodeid} tried to open a real database connection — "
            "mock the utils.db call, or move the test to tests/integration/"
        )

    monkeypatch.setattr("psycopg.connect", _refuse)
    monkeypatch.setattr("psycopg.Connection.connect", _refuse)
    # Fail fast at the pool too: a refused connect inside psycopg_pool's
    # background worker would otherwise surface as a PoolTimeout only after
    # DB_POOL_ACQUIRE_TIMEOUT seconds (code that swallows DB errors, like
    # turn persistence, would make every such test that slow).
    monkeypatch.setattr("utils.db._get_pool", _refuse)


@pytest.fixture(autouse=True)
def _no_live_alert_price(monkeypatch):
    """alert_create checks the live price (VAL-04 "already true"). Tests
    never reach yfinance for it: the price is "unknown" unless a test sets
    one, so creation proceeds as if the check couldn't run."""
    monkeypatch.setattr("graph.tool_catalogue._current_price", lambda symbol: None)


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


@pytest.fixture(autouse=True)
def _not_draining():
    """A TestClient leaving its `with` block runs the app's shutdown, which
    puts the process-wide lifecycle into draining (OPS-04). Every test starts
    from a serving state."""
    from utils import lifecycle

    lifecycle.reset()
    yield
    lifecycle.reset()


@pytest.fixture(autouse=True)
def _fresh_graph_cache():
    """Compiled graphs are cached per provider config (REL-03); a test that
    patches build_graph must not get another test's cached graph."""
    from agents.graph_orchestrator import clear_graph_cache

    clear_graph_cache()
    yield
    clear_graph_cache()
