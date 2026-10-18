"""
utils/report_files.py
Renders a report's Markdown text to downloadable bytes (.md and .pdf).
Nothing is written to disk (OPS-02): /download/report/<id> re-renders from
the durable `reports.content` (utils/db.py) on whichever replica serves the
click, so a download never depends on the replica that wrote the report.

Security (SEC-02): report text is LLM-written and shaped by user prompts,
news and filing PDFs, so it is untrusted. Raw HTML in it is sanitised to a
small allowlist of formatting tags, and WeasyPrint is given a URL fetcher that
refuses every URL — rendering never touches the
network or the local filesystem (no <img src="file:///etc/passwd">, no
<link href="http://internal/">, no CSS @import/url()).
"""

from __future__ import annotations

import functools
import logging
import re
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


def _safe_name(part: str | None, fallback: str) -> str:
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


def report_filename(sym: str | None, rtype: str | None, ext: str) -> str:
    """A download filename that can't carry path separators or '..'."""
    return f"{_safe_name(sym, 'report')}_{_safe_name(rtype, 'report')}.{ext}"


@functools.cache
def pdf_available() -> bool:
    """False when WeasyPrint's native libraries are missing (a local Windows run). The
    answer then offers Markdown only, instead of a PDF link that returns a 503."""
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


def render_report(content: str) -> tuple[bytes, bytes | None]:
    """(markdown bytes, pdf bytes or None). PDF rendering failing (missing
    native libraries) doesn't block the Markdown download."""
    content = _cap_content(content)
    md_bytes = content.encode("utf-8")
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
        pdf: bytes | None = _HTML(string=html_full, base_url=None, url_fetcher=_deny_fetcher).write_pdf()
    except Exception as e:
        # Most common local-dev cause: WeasyPrint needs native Pango/Cairo
        # libraries (the Dockerfile installs them).
        log.warning("PDF render failed, Markdown only: %s", type(e).__name__)
        pdf = None
    return md_bytes, pdf
