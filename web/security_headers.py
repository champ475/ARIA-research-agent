"""SEC-15: the standard security header set on every response.

Pure ASGI (BaseHTTPMiddleware would buffer Gradio's SSE stream). The CSP is
scoped to what Gradio needs: it injects inline scripts/styles and uses blob
workers, so `script-src` keeps 'unsafe-inline' and 'unsafe-eval'; everything
else is locked to the origin. `frame-ancestors` comes from config (default
none; set it to embed, e.g. in a Hugging Face Space).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config


def build_csp(frame_ancestors: str, turnstile: bool = False) -> str:
    script = "'self' 'unsafe-inline' 'unsafe-eval' blob:"
    frame = "'self'"
    if turnstile:  # Cloudflare Turnstile widget (SEC-05)
        script += " https://challenges.cloudflare.com"
        frame += " https://challenges.cloudflare.com"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script}",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            "img-src 'self' data: blob: https:",
            "media-src 'self' data: blob:",
            "worker-src 'self' blob:",
            "connect-src 'self' ws: wss:",
            f"frame-src {frame}",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        ]
    )


def header_set(
    frame_ancestors: str | None = None, hsts: bool | None = None, turnstile: bool | None = None
) -> list[tuple[bytes, bytes]]:
    fa = frame_ancestors if frame_ancestors is not None else config.FRAME_ANCESTORS
    use_turnstile = bool(config.TURNSTILE_SITE_KEY) if turnstile is None else turnstile
    headers = {
        "content-security-policy": build_csp(fa, use_turnstile),
        "x-content-type-options": "nosniff",
        "referrer-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    if fa == "'none'":
        headers["x-frame-options"] = "DENY"
    if config.HSTS_ENABLED if hsts is None else hsts:
        headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    return [(k.encode(), v.encode()) for k, v in headers.items()]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._headers = header_set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self._headers if k not in have]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
