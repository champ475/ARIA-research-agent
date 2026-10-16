"""
web/download_routes.py — downloads any replica can serve (OPS-02).

    GET /download/report/<id>.pdf|.md   re-rendered from reports.content
    GET /download/file/<token>          a stored file (screener CSV), 24 h

Both need the session cookie and serve only the owner's files; anything
else is a 404 (never "exists but not yours"). Nothing here touches the
replica's disk, so a link works whichever replica the click lands on, and
after a restart.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from web.session import current_session

log = logging.getLogger(__name__)

router = APIRouter()

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TYPES = {"pdf": "application/pdf", "md": "text/markdown; charset=utf-8"}


def _attachment(body: bytes, filename: str, media_type: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80] or "download"
    return Response(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _not_found() -> Response:
    return Response("Not found", status_code=404, media_type="text/plain")


@router.get("/download/report/{report_id}.{fmt}")
def download_report(report_id: int, fmt: str, request: Request) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if fmt not in _TYPES:
        return _not_found()
    from utils.db import report_get
    from utils.report_files import render_report, report_filename

    row = report_get(info.user_id, report_id)
    if row is None:
        return _not_found()
    md, pdf = render_report(row["content"])
    body = pdf if fmt == "pdf" else md
    if body is None:  # PDF engine unavailable on this host
        return Response("PDF rendering is unavailable; download the Markdown instead.", status_code=503)
    return _attachment(body, report_filename(row["symbol"], row["report_type"], fmt), _TYPES[fmt])


@router.get("/download/file/{token}")
def download_file(token: str, request: Request) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if not _TOKEN_RE.match(token):
        return _not_found()
    from utils.db import download_get

    row = download_get(info.user_id, token)
    if row is None:
        return _not_found()
    return _attachment(bytes(row["content"]), row["filename"], row["content_type"])
