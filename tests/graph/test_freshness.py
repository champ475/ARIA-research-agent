"""DATA-06: every market answer says when its figures are from; a closed day
says the figures are the last session's; Evidence carries `as_of`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from graph.freshness import answer_as_of, closed_today_header, freshness_note
from utils.helpers import IST


def ev(kind, as_of=None, **extra):
    e = {"evidence_type": kind, "timestamp": datetime(2026, 10, 15, 12, 0, tzinfo=UTC), **extra}
    if as_of:
        e["as_of"] = as_of
    return e


def test_oldest_market_figure_dates_the_answer():
    older = IST.localize(datetime(2026, 10, 15, 15, 31))
    newer = IST.localize(datetime(2026, 10, 16, 11, 2))
    state = {
        "evidence": [
            ev("quote_info", newer),
            ev("price_ohlcv", older),
            ev("news_headline", older - timedelta(days=3)),
        ]
    }
    assert answer_as_of(state) == older  # news isn't a market figure
    note = freshness_note(state, now=IST.localize(datetime(2026, 10, 16, 16, 0)))
    assert note == "\n\n🕒 *Data as of 15 Oct 2026, 15:31 IST (market closed)*"


def test_no_market_evidence_no_note():
    assert freshness_note({"evidence": [ev("news_headline")]}) == ""
    assert freshness_note({}) == ""


def test_closed_day_header_only_for_today_figures():
    sunday = IST.localize(datetime(2026, 10, 18, 11, 0))
    movers = {"evidence": [ev("top_movers")]}
    assert closed_today_header(movers, sunday) == (
        "📅 *Markets are closed today; these figures are from the last session, Fri, 16 Oct 2026.*\n\n"
    )
    dussehra = IST.localize(datetime(2026, 10, 20, 11, 0))
    assert "(Dussehra)" in closed_today_header(movers, dussehra)
    assert closed_today_header(movers, IST.localize(datetime(2026, 10, 15, 11, 0))) == ""
    assert closed_today_header({"evidence": [ev("fundamental_ratio")]}, sunday) == ""


def test_execute_task_sets_as_of_from_quote_time_and_cache():
    from graph.nodes import execute_task as et

    quoted = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert et._as_of({"regularMarketTime": quoted.timestamp()}, datetime.now(UTC)) == quoted
    fetched = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    assert et._as_of({"regularMarketTime": 9e12}, fetched) == fetched  # future: ignored
    assert et._as_of({"regularMarketTime": True}, fetched) == fetched
    assert et._as_of([1, 2], fetched) == fetched

    cached = {"payload": {"currentPrice": 10}, "confidence": 1.0, "fetched_at": fetched}
    with (
        patch.object(et.evidence_cache, "get", return_value=cached),
        patch.object(et.tracing, "current", return_value=None),
    ):
        out = et.execute_task({"tool": "fetch_info", "entity": "TCS"}, {})
    assert out["as_of"] == fetched  # a cache hit keeps the original fetch time


def test_orchestrator_answer_carries_as_of_before_the_footer():
    from tests.agents.test_graph_orchestrator import _build_orchestrator

    as_of = IST.localize(datetime(2026, 10, 15, 15, 31))
    graph = MagicMock()
    graph.invoke.return_value = {
        "final_report": "TCS closed at ₹3,000.",
        "intent": "equity",
        "evidence": [ev("quote_info", as_of)],
        "tasks": [],
    }
    orch = _build_orchestrator(graph)
    with patch("graph.freshness.closed_today_note", return_value=""):
        text = list(orch.process_stream("TCS price"))[-1][0]
    assert "🕒 *Data as of 15 Oct 2026, 15:31 IST" in text
    if "\n---\n" in text:
        assert text.index("Data as of") < text.index("\n---\n")


def test_market_wide_evidence_is_dated_by_the_last_session_close_when_closed(monkeypatch):
    from graph.nodes import execute_task as et

    close = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("utils.market_clock.last_close", lambda now=None: close)
    fetched = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
    assert et._as_of(None, fetched, "top_movers") == close
    assert et._as_of({}, fetched, "market_overview") == close
    assert et._as_of({}, fetched, "news_headline") == fetched  # not a market figure


def test_market_wide_evidence_keeps_the_fetch_time_while_open(monkeypatch):
    from graph.nodes import execute_task as et

    monkeypatch.setattr("utils.market_clock.last_close", lambda now=None: None)
    fetched = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)
    assert et._as_of(None, fetched, "top_movers") == fetched


def test_index_levels_are_dated_and_marked_as_the_last_session_when_closed():
    from datetime import UTC, datetime

    from graph.freshness import closed_today_header, freshness_note

    sat = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)  # Saturday
    state = {
        "evidence": [{"evidence_type": "market_overview", "as_of": datetime(2026, 10, 16, 10, 0, tzinfo=UTC)}]
    }
    assert "Markets are closed today" in closed_today_header(state, now=sat)
    assert "Data as of 16 Oct 2026" in freshness_note(state, now=sat)


def test_screen_results_are_dated_by_their_snapshot_not_the_run_time():
    from datetime import UTC, datetime

    from graph.nodes import execute_task as et

    rows = [
        {"symbol": "A", "as_of": "2026-10-16T16:00:00+00:00"},
        {"symbol": "B", "as_of": "2026-10-15T16:00:00+00:00"},
    ]
    fetched = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
    assert et._as_of(rows, fetched, "screener_results") == datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert et._as_of([{"symbol": "A", "as_of": None}], fetched, "screener_results") == fetched
    assert et._as_of([], fetched, "screener_results") == fetched
