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

Security (SEC-02): report text is LLM-written and shaped by user prompts,
news and filing PDFs, so it is untrusted. Raw HTML in it is sanitised to a
small allowlist of formatting tags, and WeasyPrint is given a URL fetcher that
refuses every URL — rendering never touches the
network or the local filesystem (no <img src="file:///etc/passwd">, no
<link href="http://internal/">, no CSS @import/url()).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from typing import NoReturn

log = logging.getLogger(__name__)

MAX_REPORT_BYTES = 200_000  # content beyond this is truncated before rendering
_TRUNCATION_NOTE = "\n\n---\n*Report truncated: it exceeded the maximum length for download.*\n"

# Formatting-only allowlist: what the Markdown converter legitimately emits for
# reports (headings, paragraphs, lists, tables, code, emphasis, links).
_ALLOWED_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "br",
    "hr",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "code",
    "pre",
    "em",
    "strong",
    "b",
    "i",
    "blockquote",
    "a",
    "sup",
    "sub",
    "del",
}
_ALLOWED_ATTRIBUTES = {"a": {"href"}, "th": {"align"}, "td": {"align"}}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


class BlockedFetchError(ValueError):
    """Raised by the PDF URL fetcher for any non-data: URL."""


class _DenyFetcher:
    """WeasyPrint url_fetcher that fetches nothing. Reports carry no images
    (sanitise_html strips <img> anyway), so even data: URIs aren't needed.
    WeasyPrint logs the refusal and renders without the resource.

    A callable object rather than a function: WeasyPrint 70 reads
    `url_fetcher._fail_on_errors` off whatever fetcher it is given (False =
    a refused resource is a warning, not a failed render), and a bare
    function has no such attribute. Older versions just call it."""

    _fail_on_errors = False

    def __call__(self, url: str, *args: object, **kwargs: object) -> NoReturn:
        raise BlockedFetchError(f"external resource blocked in report rendering: {url[:80]}")

    def fetch(self, url: str, headers: object = None) -> NoReturn:
        self(url)


_deny_fetcher = _DenyFetcher()


def _safe_name(part: str, fallback: str) -> str:
    """Filename-safe slug — `sym`/`rtype` can carry user/LLM text; never let
    them add path separators or '..' to the temp path."""
    cleaned = _SAFE_NAME_RE.sub("", part or "")[:40]
    return cleaned or fallback


def _cap_content(content: str) -> str:
    data = (content or "").encode("utf-8")
    if len(data) <= MAX_REPORT_BYTES:
        return content or ""
    return data[:MAX_REPORT_BYTES].decode("utf-8", errors="ignore") + _TRUNCATION_NOTE


def sanitise_html(html_body: str) -> str:
    """Strip every tag/attribute outside the formatting allowlist; drop the
    contents of script/style entirely; links keep only http(s)/mailto hrefs."""
    import nh3

    return nh3.clean(
        html_body,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        clean_content_tags={"script", "style"},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


def render_report(sym: str, rtype: str, content: str) -> tuple[str | None, str | None]:
    """Renders `content` as Markdown and (optionally) PDF into temp files.
    Returns (md_path, pdf_path) — either may be None if that format failed
    to render (PDF rendering failing shouldn't block the Markdown download)."""
    content = _cap_content(content)
    filename = f"{_safe_name(sym, 'report')}_{_safe_name(rtype, 'report')}_{uuid.uuid4().hex[:8]}"
    base = os.path.join(tempfile.gettempdir(), filename)
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

        html_body = sanitise_html(_md.markdown(content, extensions=["tables", "fenced_code"]))
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
        _HTML(string=html_full, base_url=None, url_fetcher=_deny_fetcher).write_pdf(pdf_path)
    except Exception as e:
        # Deliberately non-fatal (see module docstring) — but silent
        # failure here means "why is there no PDF button" has zero signal
        # for whoever's debugging. Most common local-dev cause: WeasyPrint
        # needs native Pango/Cairo/GTK libs not on PATH (Dockerfile already
        # installs them for the actual deployment target).
        log.warning(f"PDF render failed, falling back to Markdown-only: {e}")
        pdf_path = None

    return md_path, pdf_path
