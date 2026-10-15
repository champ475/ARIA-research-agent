"""
server.py — the ASGI app: FastAPI routes around the mounted Gradio UI (SEC-04).

    /login, /signup, /terms, /auth/logout   web/auth_routes.py (server-rendered)
    /healthz, /readyz                        web/ops_routes.py (OPS-04)
    /                                        Gradio (app.build_ui), cookie-authenticated

Gradio is mounted with `auth_dependency`, so every Gradio route (page config,
queue, file downloads) resolves the user from the session cookie on each
request; a logged-out visitor opening the page is redirected to /login.

Run with `python app.py` (which calls serve()).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import gradio as gr
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config
from utils import lifecycle
from web import account_routes, auth_routes, ops_routes
from web.session import COOKIE_NAME, current_session, gradio_auth_dependency

log = logging.getLogger(__name__)

# Gradio's page entry points: a visitor without a session goes to /login
# instead of Gradio's bare 401 JSON.
_PAGE_PATHS = {"/", ""}


class LoginRedirectMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware, which buffers streaming responses
    such as Gradio's SSE queue): only intercepts GET of the page itself."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "GET" and scope.get("path") in _PAGE_PATHS:
            from starlette.requests import Request
            from starlette.responses import RedirectResponse

            request = Request(scope)
            if gradio_auth_dependency(request) is None:
                if current_session(request) is not None:
                    # Logged in, email not confirmed yet (SEC-06).
                    response = RedirectResponse("/auth/verify-pending", status_code=303)
                else:
                    response = RedirectResponse("/login", status_code=303)
                    if request.cookies.get(COOKIE_NAME):
                        response.delete_cookie(COOKIE_NAME, path="/")  # expired or revoked
                await response(scope, receive, send)
                return

            # The app page must never come back from the browser cache: after
            # logout, the back button has to re-request it (and be redirected).
            async def send_no_store(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_no_store)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    # OPS-04 graceful shutdown: no new turns, let running ones finish (up to
    # SHUTDOWN_GRACE_SECONDS), then close the database pools.
    lifecycle.begin_drain()
    await asyncio.to_thread(lifecycle.wait_idle, lifecycle.SHUTDOWN_GRACE_SECONDS)
    from graph.checkpointer import close_checkpointer
    from utils.db import close_pool

    close_checkpointer()
    close_pool()
    log.info("[server] shutdown complete")


def create_app(demo: gr.Blocks | None = None) -> FastAPI:
    """The production ASGI app. `demo` defaults to app.build_ui()."""
    from app import APP_CSS, APP_JS, build_ui

    api = FastAPI(title="ARIA", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    api.include_router(ops_routes.router)
    api.include_router(auth_routes.router)
    api.include_router(account_routes.router)
    api.add_middleware(LoginRedirectMiddleware)
    demo = demo if demo is not None else build_ui()
    kwargs: dict[str, Any] = {
        "auth_dependency": gradio_auth_dependency,
        "theme": gr.themes.Base(),
        "css": APP_CSS,
        "js": APP_JS,
        # SEC-08: never show raw handler exceptions in Gradio's error toast.
        "show_error": False,
    }
    return gr.mount_gradio_app(api, demo, path="/", **kwargs)


def serve() -> None:
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            # OPS-04: on SIGTERM/SIGINT flip to draining *first*, so /readyz
            # turns 503 and new chat turns are refused while uvicorn stops
            # accepting connections and waits for open ones.
            lifecycle.begin_drain()
            super().handle_exit(sig, frame)

    from utils.maintenance import start_housekeeping_thread

    start_housekeeping_thread()
    cfg = uvicorn.Config(
        create_app(),
        host=config.HOST,
        port=config.PORT,
        log_config=None,
        proxy_headers=False,
        timeout_graceful_shutdown=int(lifecycle.SHUTDOWN_GRACE_SECONDS),
    )
    _Server(cfg).run()
