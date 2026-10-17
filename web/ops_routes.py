"""
web/ops_routes.py — liveness and readiness (OPS-04).

    GET /healthz   200 while the process can answer HTTP at all (liveness:
                   restart the container if this fails).
    GET /health/data  per-source data health (DATA-02): breaker state, counts,
                   last success/failure.
    GET /metrics   Prometheus metrics (OBS-03); needs `Authorization: Bearer
                   <METRICS_TOKEN>` — outside dev/test the route is off
                   without a token.
    GET /readyz    200 only when this instance should get traffic: the
                   database answers, the schema is at the Alembic head, at
                   least one LLM provider is configured, and it isn't
                   draining for shutdown. 503 lists what is wrong.

The others are unauthenticated and reveal no user data or error text.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

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
    # ARIA_FAKE_LLM (tests, demos, load runs) is refused when APP_ENV=prod, so it can't mask a missing key there.
    return bool(
        config.GROQ_API_KEYS or config.GEMINI_API_KEY or config.LOCAL_LLM_BASE_URL or config.ARIA_FAKE_LLM
    )


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


@router.get("/health/data")
def health_data() -> JSONResponse:
    """DATA-02: each external data source's breaker state, call counts and
    last success/failure in this process. 200 always (a source being down
    is information, not this instance being unready); `degraded` lists the
    sources currently skipped."""
    from tools.data.providers import market_data
    from utils.resilience import breaker_states

    sources = breaker_states()
    provider = market_data()
    routes = getattr(provider, "routes", None)
    return JSONResponse(
        {
            "provider": getattr(provider, "name", "unknown"),
            "routes": {m: p.name for m, p in routes.items()} if routes else None,
            "degraded": [name for name, s in sources.items() if s["state"] != "closed"],
            "sources": sources,
        }
    )


@router.get("/metrics")
def metrics_route(request: Request) -> Response:
    import secrets

    from prometheus_client import CONTENT_TYPE_LATEST

    from utils import metrics

    token = config.METRICS_TOKEN
    if not token:
        if config.APP_ENV not in ("dev", "test"):
            return Response("Not found", status_code=404, media_type="text/plain")
    elif not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
        return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)
