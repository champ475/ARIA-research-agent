"""Phase 7.4: services/chat.py::tab_label() renders the routing badge, including a
joined multi-domain label when GraphOrchestrator's tab_used names more than
one domain (e.g. "report+alert")."""

from services.chat import tab_label as _tab_label


def test_single_known_domain_renders_its_label():
    assert _tab_label("equity") == "📊 Equity Research"


def test_unknown_domain_falls_back_to_raw_string():
    assert _tab_label("chat") == "chat"


def test_joined_domains_render_both_labels():
    result = _tab_label("report+alert")
    assert "Report Generator" in result
    assert "Alerts" in result
    assert " + " in result
