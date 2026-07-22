"""Shared fixtures for the graph test suite."""
import os
from datetime import datetime, timezone

import pytest


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
        "timestamp": datetime.now(timezone.utc),
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
