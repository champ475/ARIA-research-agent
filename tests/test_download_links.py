"""OPS-02: the chat's download links point only at our own download routes."""

import pytest

from tests.conftest import import_app_without_side_effects

app = import_app_without_side_effects()


def test_links_to_our_routes_are_rendered_escaped():
    html = app._download_link("/download/report/12.pdf", "⬇ Download PDF", primary=True)
    assert html == '<a class="dl-btn dl-primary" href="/download/report/12.pdf" download>⬇ Download PDF</a>'
    assert 'href="/download/file/' in app._download_link("/download/file/" + "a" * 32, "CSV")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x.pdf",
        "javascript:alert(1)",
        "/download/report/12.pdf?x=1",
        '/download/report/1.pdf" onclick="x',
        "/tmp/aria_screener_results.csv",
        "",
    ],
)
def test_anything_else_renders_nothing(url):
    assert app._download_link(url, "x") == ""
