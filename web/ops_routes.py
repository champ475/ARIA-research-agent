"""
web/ops_routes.py — liveness and readiness (OPS-04).

    GET /healthz   200 while the process can answer HTTP at all (liveness:
                   restart the container if this fails).
    GET /readyz    200 only when this instance should get traffic: the
                   database answers, the schema is at the Alembic head, at
                   least one LLM provider is configured, and it isn't
                   draining for shutdown. 503 lists what is wrong.

Both are unauthenticated and reveal nothing beyond pass/fail per check.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config
from utils import lifecycle

log = logging.getLogger(__name__)

router = APIRouter()

_STARTED = time.time()


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "uptime_s": int(time.time() - _STARTED)}


def _check_database() -> str | None:
    from utils import db, migrations

    conn = db.get_conn()
    try:
        conn.execute("SELECT 1")
        migrations.check_schema(conn)
    except migrations.SchemaOutOfDate:
        return "schema not at the latest migration"
    finally:
        db.release_conn(conn)
    return None


def _llm_configured() -> bool:
    return bool(config.GROQ_API_KEYS or config.GEMINI_API_KEY or config.LOCAL_LLM_BASE_URL)


@router.get("/readyz")
def readyz() -> JSONResponse:
    checks: dict[str, str] = {}
    if lifecycle.is_draining():
        checks["lifecycle"] = "draining for shutdown"
    try:
        problem = _check_database()
    except Exception as e:
        log.warning("[readyz] database check failed: %s", type(e).__name__)
        problem = "database unreachable"
    if problem:
        checks["database"] = problem
    if not _llm_configured():
        checks["llm"] = "no LLM provider configured"
    ok = not checks
    return JSONResponse(
        {
            "status": "ready" if ok else "not_ready",
            "problems": checks,
            "in_flight_turns": lifecycle.in_flight(),
        },
        status_code=200 if ok else 503,
    )
