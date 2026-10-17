"""UX-07: every researched answer carries a Sources block with the source, the
as-of time (IST) and the confidence, and the cited calculations say where they
came from. Code writes it; the model is never asked to."""

from __future__ import annotations

from datetime import UTC, datetime

from agents.conversation_memory import strip_sources
from graph.provenance import confidence_text, footnote_source, source_of, sources_block, used_evidence

T = datetime(2026, 10, 15, 10, 1, tzinfo=UTC)  # 15:31 IST


def _ev(eid, tool, etype, entity="TCS", confidence=1.0, as_of=T):
    return {
        "id": eid,
        "tool": tool,
        "source": tool,
        "entity": entity,
        "evidence_type": etype,
        "confidence": confidence,
        "timestamp": as_of,
        "as_of": as_of,
        "payload": {},
        "citation_key": etype,
    }


STATE = {
    "evidence": [
        _ev("e1", "fetch_info", "quote_info"),
        _ev("e2", "compute_fundamentals", "fundamental_ratio"),
        _ev("e3", "fetch_news", "news_headline", confidence=0.6),
        _ev("e4", "fetch_ohlcv", "price_ohlcv"),  # fetched but not cited
    ],
    "conclusions": [
        {
            "claim": "TCS price",
            "value": 1,
            "confidence": 1.0,
            "evidence_ids": ["e1", "e2"],
            "citation_key": "p",
        },
        {"claim": "news", "value": 1, "confidence": 0.6, "evidence_ids": ["e3", "e1"], "citation_key": "n"},
    ],
}


def test_block_lists_each_cited_evidence_once_with_source_time_and_confidence():
    block = sources_block(STATE)
    assert block.startswith("\n\n<details><summary>Sources (3)</summary>") and block.endswith("</details>")
    assert (
        "| Quote and company info · TCS | Yahoo Finance (NSE end-of-day file if unavailable) | 15 Oct 2026, 15:31 IST | full |"
        in block
    )
    assert "Fundamental ratios · TCS" in block
    assert "Publisher RSS feeds and Google News" in block
    assert "Price history" not in block  # fetched, but no conclusion rests on it


def test_reduced_confidence_says_why():
    block = sources_block(STATE)
    assert "reduced (60%): the usual source was unavailable, so a fallback stood in" in block
    assert confidence_text(1.0) == "full"


def test_no_evidence_no_block():
    assert sources_block({}) == ""
    assert sources_block({"evidence": [], "conclusions": []}) == ""


def test_untrusted_text_cannot_break_the_table():
    state = {
        "evidence": [_ev("e1", "fetch_info", "quote_info", entity="X|<img src=x>\n| y")],
        "conclusions": [
            {"claim": "c", "value": 1, "confidence": 1, "evidence_ids": ["e1"], "citation_key": "k"}
        ],
    }
    block = sources_block(state)
    rows = [line for line in block.splitlines() if line.startswith("| Quote")]
    assert len(rows) == 1 and rows[0].count("|") == 5 and "<img" not in rows[0]


def test_sources_are_chosen_by_tool():
    assert source_of("fetch_nav") == "AMFI NAV file and mfapi.in"
    assert source_of("screener_run").startswith("ARIA's nightly screener")
    assert source_of("fetch_bulk_deals") == "NSE India"
    assert source_of("mystery") == "ARIA"


def test_used_evidence_falls_back_to_nothing_without_conclusions():
    assert used_evidence({"evidence": STATE["evidence"]}) == []
    assert [e["id"] for e in used_evidence(STATE)] == ["e1", "e2", "e3"]


def test_footnote_source_line_names_source_time_and_confidence():
    line = footnote_source(STATE, "TCS")
    assert "Source: Yahoo Finance data, calculated by ARIA" in line and "15 Oct 2026, 15:31 IST" in line
    assert footnote_source(STATE, "INFY") == ""


def test_block_is_stripped_before_the_model_sees_history():
    text = "TCS looks fine.\n\n---\n*footer*" + sources_block(STATE) + "\n\nℹ️ note"
    out = strip_sources(text)
    assert "Sources" not in out and "TCS looks fine." in out and out.endswith("ℹ️ note")
