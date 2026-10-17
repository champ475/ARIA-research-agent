"""TEST-03: every row of tests/edge_cases/matrix.yaml is a named test, run
through the real code by `kind`. Adding a row needs no Python change."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from utils.helpers import IST

HERE = Path(__file__).parent
ROWS: list[dict[str, Any]] = yaml.safe_load((HERE / "matrix.yaml").read_text(encoding="utf-8"))


def _input_guard(row: dict, exp: dict) -> None:
    from utils.input_guard import CleanMessage, InputRejected, clean_message

    text = row.get("input")
    if "input_repeat" in row:
        text = row["input_repeat"]["text"] * row["input_repeat"]["times"]
    out = clean_message(text)
    if exp["status"] == "rejected":
        assert isinstance(out, InputRejected) and out.reason == exp["reason"]
        assert not re.search(r"rate[- ]?limit", out.user_message, re.I)  # never labelled a rate limit
    else:
        assert isinstance(out, CleanMessage)
        if "text" in exp:
            assert out.text == exp["text"]


def _entities(row: dict, exp: dict) -> None:
    from graph.entity_resolution import add_llm_mentions, resolve_mentions

    out = resolve_mentions(row["query"])
    if row.get("llm_mentions"):  # what QueryAnalyzer's model reports as company names
        add_llm_mentions(out, row["llm_mentions"])
    if "entities" in exp:
        assert out.entities == exp["entities"]
    if "unresolved_status" in exp:
        assert [u["status"] for u in out.unresolved] == [exp["unresolved_status"]]
    if "suggestions_include" in exp:
        suggested = [c["symbol"] for u in out.unresolved for c in u["candidates"]] + out.entities
        assert exp["suggestions_include"] in suggested


def _entity_cap(row: dict, exp: dict) -> None:
    from graph.entity_resolution import blocking_resolutions
    from graph.nodes.query_analyzer import build_query_analyzer_node

    client = MagicMock()
    client.ask.return_value = json.dumps({"intent": "equity", "complexity": "moderate", "companies": []})
    state = build_query_analyzer_node(client)({"query": row["query"], "history": []})
    assert [b["status"] for b in blocking_resolutions(state)] == [exp["blocking_status"]]


def _alert(row: dict, exp: dict) -> None:
    from graph.alert_spec import AlertRefusal, AlertSpec, parse_alert

    out = parse_alert(row["message"], row["symbol"])
    if "refused_contains" in exp:
        assert isinstance(out, AlertRefusal) and exp["refused_contains"] in out.reason
        return
    assert isinstance(out, AlertSpec)
    assert out.alert_type == exp["alert_type"] and out.threshold == exp["threshold"]
    if "direction" in exp:
        assert out.direction == exp["direction"]


def _alert_already(row: dict, exp: dict) -> None:
    from graph.alert_spec import already_true, parse_alert

    assert already_true(parse_alert(row["message"], row["symbol"]), row["price"]) is exp["already_true"]


def _alert_cancel(row: dict, exp: dict) -> None:
    from graph import tool_catalogue as tc

    alerts = [
        {**a, "active": True, "condition": f"alert {a['id']}", "alert_type": "price_above"}
        for a in row["alerts"]
    ]
    with (
        patch.object(tc, "db_alert_list", return_value=alerts),
        patch.object(tc, "db_alert_remove"),
        patch.object(tc, "audit"),
    ):
        out = tc._alert_cancel("TCS", {"_user_id": 1, "_query": row["query"]})
    assert out["result"] == exp["result"]
    if "matches" in exp:
        assert len(out["matches"]) == exp["matches"]
    if "alert_id" in exp:
        assert out["alert_id"] == exp["alert_id"]


def _wants_build(row: dict, exp: dict) -> None:
    from utils.financial_profile import wants_build

    assert wants_build(row["message"]) is exp["build"]


def _amount(row: dict, exp: dict) -> None:
    from utils.numbers import parse_inr_amount

    p = parse_inr_amount(row["text"])
    assert p is not None and float(p.value) == pytest.approx(exp["value"])


def _confirmation(row: dict, exp: dict) -> None:
    from graph.confirmation import classify_reply

    assert classify_reply(row["reply"]) == exp["kind"]


def _report_id(row: dict, exp: dict) -> None:
    from graph.nodes.communication_layer import _REPORT_ID_RE

    m = _REPORT_ID_RE.search(row["query"])
    assert (int(m.group(1)) if m else None) == exp["report_id"]


def _per_session(row: dict, exp: dict) -> None:
    from tests.conftest import import_app_without_side_effects

    app = import_app_without_side_effects()
    with (
        patch("agents.graph_orchestrator.GraphOrchestrator", side_effect=lambda *a, **k: object()),
        patch.object(app, "_orchestrators", app._orchestrators.__class__(maxsize=10, ttl=60)),
    ):
        a, b = app.get_orchestrator(7, 1), app.get_orchestrator(7, 2)
        assert (a is not b) is exp["distinct"] and app.get_orchestrator(7, 1) is a


def _watchlist(row: dict, exp: dict) -> None:
    from utils.errors import UserInputError
    from utils.escaping import html_text
    from utils.forms import watchlist_symbol

    with pytest.raises(UserInputError):
        watchlist_symbol(row["input"])
    if "escaped_contains" in exp:
        assert exp["escaped_contains"] in html_text(row["input"])


def _market_closed(row: dict, exp: dict) -> None:
    from graph.freshness import closed_today_header

    now = IST.localize(datetime.fromisoformat(row["at"]))
    state = {"evidence": [{"evidence_type": t} for t in row["evidence"]]}
    assert exp["header_contains"] in closed_today_header(state, now)


def _golden_coverage(row: dict, exp: dict) -> None:
    golden = Path(__file__).parents[2] / "evals" / "cases" / "golden.jsonl"
    cases = [json.loads(line) for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    mine = [c for c in cases if c["domain"] == row["domain"]]
    assert len(mine) >= row["min_cases"]


KINDS = {
    "input_guard": _input_guard,
    "entities": _entities,
    "entity_cap": _entity_cap,
    "alert": _alert,
    "alert_already": _alert_already,
    "alert_cancel": _alert_cancel,
    "wants_build": _wants_build,
    "amount": _amount,
    "confirmation": _confirmation,
    "report_id": _report_id,
    "per_session": _per_session,
    "watchlist": _watchlist,
    "market_closed": _market_closed,
    "golden_coverage": _golden_coverage,
}


def test_matrix_is_well_formed():
    ids = [r["id"] for r in ROWS]
    assert len(ids) == len(set(ids)), "row ids must be unique"
    for r in ROWS:
        assert r["kind"] in KINDS, f"{r['id']}: unknown kind {r['kind']!r}"
        assert r.get("req"), f"{r['id']}: needs the PRD requirement it covers"
    # Every requirement in the PRD's matrix has at least one row.
    covered = {r["req"] for r in ROWS}
    assert {
        "VAL-01",
        "VAL-02",
        "VAL-03",
        "VAL-04",
        "VAL-05",
        "VAL-07",
        "VAL-08",
        "VAL-09",
        "VAL-10",
        "DATA-06",
    } <= covered


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_edge_case(row):
    KINDS[row["kind"]](row, row.get("expect") or {})
