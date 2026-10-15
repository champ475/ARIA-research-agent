"""
server.py — the ASGI app: FastAPI routes around the mounted Gradio UI (SEC-04).

    /login, /signup, /terms, /auth/logout   web/auth_routes.py (server-rendered)
    /                                        Gradio (app.build_ui), cookie-authenticated

Gradio is mounted with `auth_dependency`, so every Gradio route (page config,
queue, file downloads) resolves the user from the session cookie on each
request; a logged-out visitor opening the page is redirected to /login.

Run with `python app.py` (which calls serve()).
"""

from __future__ import annotations

import logging
from typing import Any

import gradio as gr
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config
from web import auth_routes
from web.session import COOKIE_NAME, gradio_auth_dependency

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


def create_app(demo: gr.Blocks | None = None) -> FastAPI:
    """The production ASGI app. `demo` defaults to app.build_ui()."""
    from app import APP_CSS, APP_JS, build_ui

    api = FastAPI(title="ARIA", docs_url=None, redoc_url=None, openapi_url=None)
    api.include_router(auth_routes.router)
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

    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_config=None, proxy_headers=False)
