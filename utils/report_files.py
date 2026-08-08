"""
utils/report_files.py
Renders a report's Markdown text into on-disk .md/.pdf files for exactly one
Gradio DownloadButton response — never a durable store. The report's actual
persistence is Postgres (`reports.content`, utils/db.py); these files live
in the system temp dir just long enough to be streamed to the user's own
machine, then are the OS's problem, not ours (fine on an ephemeral host that
wipes on restart, and cheap enough elsewhere not to bother sweeping).
Called both for a freshly generated report (graph/nodes/communication_layer.py)
and for re-downloading an older one from history (app.py), via the same
function — there's exactly one render path, not two.
"""
from __future__ import annotations

import os
import tempfile
import uuid


def render_report(sym: str, rtype: str, content: str) -> tuple[str | None, str | None]:
    """Renders `content` as Markdown and (optionally) PDF into temp files.
    Returns (md_path, pdf_path) — either may be None if that format failed
    to render (PDF rendering failing shouldn't block the Markdown download)."""
    base = os.path.join(tempfile.gettempdir(), f"{sym}_{rtype}_{uuid.uuid4().hex[:8]}")
    md_path = pdf_path = None

    try:
        md_path = base + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        md_path = None

    try:
        import markdown as _md
        from weasyprint import HTML as _HTML
        html_body = _md.markdown(content, extensions=["tables", "fenced_code"])
        html_full = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            "body{font-family:Arial,sans-serif;font-size:12px;margin:40px;color:#222}"
            "h1,h2,h3{color:#1a1a2e}"
            "table{border-collapse:collapse;width:100%;margin:12px 0}"
            "th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}"
            "th{background:#f0f4f8;font-weight:bold}"
            "code{background:#f5f5f5;padding:2px 4px;border-radius:3px;font-size:11px}"
            f"</style></head><body>{html_body}</body></html>"
        )
        pdf_path = base + ".pdf"
        _HTML(string=html_full).write_pdf(pdf_path)
    except Exception:
        pdf_path = None

    return md_path, pdf_path
