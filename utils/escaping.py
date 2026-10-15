"""
utils/escaping.py — make stored or third-party text safe to render (SEC-10).

Use these for every dynamic value placed in HTML (gr.HTML, templates built by
hand) or in a Markdown table that the Chatbot renders. Values come from users
(watchlist symbols, report titles, session names), from data providers
(company names) or from the LLM, so none of them can be trusted as markup.
"""

from __future__ import annotations

import html


def html_text(value: object) -> str:
    """Text for an HTML element body or attribute value."""
    return html.escape("" if value is None else str(value), quote=True)


def md_cell(value: object, max_len: int = 200) -> str:
    """Text for one Markdown table cell: HTML-escaped, no pipes or newlines
    (which would break out of the cell or the table)."""
    text = " ".join(("" if value is None else str(value)).split())[:max_len]
    return html_text(text).replace("|", "&#124;")
