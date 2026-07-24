"""
utils/report_files.py
Shared Markdown/PDF file-writer for generated equity research reports.
Extracted from agents/report_agent.py::_save_files() (Phase 7.1 of the
graph-expansion plan) so both the legacy ReportAgent and the LangGraph
CommunicationLayer node produce byte-identical output from the same code
path during the coexistence window (both orchestrators run in parallel
behind USE_LANGGRAPH_ORCHESTRATOR) — avoids maintaining two renderers.
"""
from __future__ import annotations

import os

from config import REPORTS_DIR
from utils.helpers import now_ist


def save_report_files(sym: str, rtype: str, content: str) -> tuple[str | None, str | None]:
    """Save report as Markdown and (optionally) PDF. Returns (md_path, pdf_path)."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts   = now_ist().replace(" ", "_").replace(",", "").replace(":", "")
    base = os.path.join(REPORTS_DIR, f"{sym}_{rtype}_{ts[:15]}")
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
