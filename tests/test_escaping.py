"""SEC-10: dynamic values are escaped wherever they become HTML or Markdown."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from utils.escaping import html_text, md_cell

PAYLOADS = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "<iframe src=//evil>"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_watchlist_cells_render_as_text(app_module, payload):
    html = app_module._watchlist_table_html([[payload, payload, "1", "2", "3", "4", "5"]])
    assert "<script" not in html and "<img" not in html and "<iframe" not in html
    assert "&lt;" in html


def test_md_cell_cannot_break_out_of_a_table_row():
    cell = md_cell("a | b\n| c | <b>x</b>")
    assert "|" not in cell and "\n" not in cell and "<b>" not in cell


def test_html_text_quotes_attributes():
    assert html_text('" onmouseover="x') == "&quot; onmouseover=&quot;x"


def test_saved_report_titles_are_escaped_in_the_list(app_module):
    from graph.nodes import communication_layer as cl

    evil = {
        "id": 7,
        "report_type": "ic",
        "title": "<img src=x onerror=1> | pwn",
        "created_at": datetime(2026, 10, 15),
    }
    with patch.object(cl, "report_list", return_value=[evil]):
        out = cl._handle_report_management({"query": "list my reports", "user_id": 1})
    text = out["final_report"]
    assert "<img" not in text and "pwn" in text
    row = next(line for line in text.splitlines() if line.startswith("| 7 |"))
    assert row.count("|") == 5  # the title's pipe didn't add a column


def test_chatbot_sanitises_html_explicitly(app_module):
    import gradio as gr

    demo = app_module.build_ui()
    bots = [b for b in demo.blocks.values() if isinstance(b, gr.Chatbot)]
    assert bots and all(b.sanitize_html for b in bots)
