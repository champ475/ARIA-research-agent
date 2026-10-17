"""OPS-02: download links in answers point only at our own download routes."""

import pytest

from services import chat as chat_svc
from tests.conftest import import_app_without_side_effects

app = import_app_without_side_effects()


def test_links_to_our_routes():
    token = "a" * 32
    line = chat_svc.download_links(
        "/download/report/12.pdf", "/download/report/12.md", f"/download/file/{token}"
    )
    assert line == (
        "\n\n[⬇ Download PDF](/download/report/12.pdf) · [⬇ Download Markdown](/download/report/12.md)"
        f" · [⬇ Export results (CSV)](/download/file/{token})"
    )
    assert chat_svc.download_links(None, None, None) == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x.pdf",
        "javascript:alert(1)",
        "/download/report/12.pdf?x=1",
        "/download/report/1.pdf) [x](https://evil.example",
        "/tmp/aria_screener_results.csv",
        "",
    ],
)
def test_anything_else_renders_nothing(url):
    assert chat_svc.download_link(url, "x") == ""
