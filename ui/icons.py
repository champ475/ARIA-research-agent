"""
ui/icons.py — ARIA's icon set: one stroke style (Lucide-like, 24px grid), two uses.

- `css()` turns every icon into a CSS mask variable (`--i-NAME`) and a class
  (`.ico-NAME`) so a Gradio button or tab can show one through `::before`
  without changing its text (e2e tests and screen readers keep the label).
- `svg(name)` returns the inline SVG for server-rendered HTML (header, auth pages).

Icons draw with `currentColor` (inline) or as a mask filled by the element's text
colour (CSS), so no colour lives here; the `--aria-*` tokens decide (UX-03).
"""

from __future__ import annotations

from urllib.parse import quote

# Inner SVG markup on a 24×24 grid; attributes use single quotes (they go into CSS url("…")).
ICONS: dict[str, str] = {
    "chat": "<path d='M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'/>",
    "watchlist": "<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
    "portfolio": "<path d='M21.2 15.9A10 10 0 1 1 8 2.8'/><path d='M22 12A10 10 0 0 0 12 2v10z'/>",
    "alerts": "<path d='M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9'/><path d='M10.3 21a1.9 1.9 0 0 0 3.4 0'/>",
    "settings": "<path d='M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6'/>",
    "admin": "<path d='M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'/>",
    "send": "<path d='M12 19V5M5 12l7-7 7 7'/>",
    "sun": "<circle cx='12' cy='12' r='4'/><path d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4'/>",
    "moon": "<path d='M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z'/>",
    "panel": "<rect x='3' y='3' width='18' height='18' rx='2'/><path d='M9 3v18'/>",
    "pen": "<path d='M12 20h9'/><path d='M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z'/>",
    "x": "<path d='M18 6 6 18M6 6l12 12'/>",
    "plus": "<path d='M12 5v14M5 12h14'/>",
    "minus": "<path d='M5 12h14'/>",
    "search": "<circle cx='11' cy='11' r='7'/><path d='m20 20-3.5-3.5'/>",
    "refresh": "<path d='M21 12a9 9 0 1 1-3-6.7L21 8'/><path d='M21 3v5h-5'/>",
    "logout": "<path d='M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9'/>",
    "trash": "<path d='M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'/>",
    "chevron": "<path d='m6 9 6 6 6-6'/>",
    "check": "<path d='M20 6 9 17l-5-5'/>",
    "pause": "<path d='M8 5v14M16 5v14'/>",
    "play": "<path d='M7 4l13 8-13 8z'/>",
    "save": "<path d='M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z'/><path d='M7 3v6h8M7 21v-7h10v7'/>",
    "link": "<path d='M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7'/><path d='M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7'/>",
    "upload": "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12'/>",
    "plug": "<path d='M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z'/>",
    "info": "<circle cx='12' cy='12' r='9'/><path d='M12 16v-4M12 8h.01'/>",
    "download": "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3'/>",
}

_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='{stroke}' "
    "stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'{extra}>{body}</svg>"
)


def svg(name: str, size: int = 16) -> str:
    """Inline SVG for HTML; draws in the surrounding text colour, hidden from screen readers."""
    extra = f" width='{size}' height='{size}' aria-hidden='true' focusable='false'"
    return _SVG.format(stroke="currentColor", extra=extra, body=ICONS[name])


def _data_uri(name: str) -> str:
    # A mask only reads alpha, so any opaque stroke works; "black" keeps hex out of the stylesheet.
    return "data:image/svg+xml," + quote(
        _SVG.format(stroke="black", extra="", body=ICONS[name]), safe=" ='/:,.-"
    )


def css() -> str:
    """`:root { --i-NAME: url(...) }` plus `.ico-NAME { --ico: var(--i-NAME) }` for every icon."""
    variables = "\n".join(f'    --i-{n}: url("{_data_uri(n)}");' for n in ICONS)
    classes = "\n".join(f".ico-{n} {{ --ico: var(--i-{n}); }}" for n in ICONS)
    return f"\n/* Icons (ui/icons.py) */\n:root {{\n{variables}\n}}\n{classes}\n"
