"""
ui/auth.py — who is looking at the page (SEC-04).

Identity comes from the session cookie that server.py's auth_dependency
checked on this very request, never from anything the page sends.
"""

from __future__ import annotations

import gradio as gr


def request_user(request: gr.Request | None) -> int | None:
    """The user server.py's auth_dependency resolved from the session cookie
    for this request (SEC-04), or None outside the authenticated server."""
    username = getattr(request, "username", None) if request is not None else None
    return int(username) if username and str(username).isdigit() else None


def identify_user(request: gr.Request) -> int | None:
    """The authenticated user id for this page load (SEC-04)."""
    return request_user(request)
