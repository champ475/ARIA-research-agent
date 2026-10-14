"""DATA-01 — the shared evidence cache must never skip a write, serve one
user's data to another, or answer one query with another query's result."""

import dataclasses
import inspect
from unittest.mock import MagicMock, patch

import pytest

from graph.nodes.execute_task import execute_task, execute_task_node
from graph.tool_catalogue import get_tool, list_tools
from tools.data import evidence_cache

# Context keys that carry per-user or per-query input (injected by
# research_executor._build_context), rather than a dependency's payload.
_PER_CALL_KEYS = ("_query", "_user_id", "_llm_config")


def _reads_per_call_context(spec) -> list[str]:
    src = inspect.getsource(spec.fn)
    return [k for k in _PER_CALL_KEYS if f'"{k}"' in src]


def test_no_side_effect_tool_is_cacheable():
    offenders = [t.name for t in list_tools() if t.side_effect and t.cacheable]
    assert offenders == []


def test_every_tool_reading_user_or_query_is_keyed_on_it_or_uncached():
    """A cacheable tool whose output depends on _query/_user_id must add that
    input to its cache key; otherwise it must not be cacheable."""
    offenders = []
    for spec in list_tools():
        reads = _reads_per_call_context(spec)
        if reads and spec.cacheable and spec.cache_key is None:
            offenders.append((spec.name, reads))
        if "_user_id" in reads and spec.cacheable:
            offenders.append((spec.name, "user-scoped but cacheable"))
    assert offenders == []


@pytest.mark.parametrize(
    "tool", ["alert_create", "alert_cancel", "alert_pause", "alert_resume", "alert_list", "screener_save"]
)
def test_write_and_user_scoped_tools_never_touch_the_cache(tool):
    spec = dataclasses.replace(get_tool(tool), fn=MagicMock(return_value={"result": "ok"}))
    task = {"id": f"{tool}:TCS", "tool": tool, "entity": "TCS", "description": "", "depends_on": [], "status": "pending"}
    with (
        patch("graph.nodes.execute_task.get_tool", return_value=spec),
        patch("graph.nodes.execute_task.evidence_cache.get") as get,
        patch("graph.nodes.execute_task.evidence_cache.put") as put,
    ):
        execute_task(task, {"_user_id": 1, "_query": "x"})
        execute_task(task, {"_user_id": 1, "_query": "x"})
    get.assert_not_called()
    put.assert_not_called()
    assert spec.fn.call_count == 2  # the write runs every time


def _key_for(tool: str, context: dict) -> str:
    spec = get_tool(tool)
    parts = spec.cache_key("market", context) if spec.cache_key else {}
    return evidence_cache.compute_args_hash("market", **parts)


def test_top_losers_and_top_gainers_get_different_keys():
    assert _key_for("fetch_top_movers", {"_query": "top gainers today"}) != _key_for(
        "fetch_top_movers", {"_query": "top losers today"}
    )


def test_movers_key_matches_the_direction_the_tool_actually_fetches():
    with patch("graph.tool_catalogue.get_top_movers") as gtm:
        get_tool("fetch_top_movers").fn("market", {"_query": "Top LOSERS"})
    gtm.assert_called_once_with("losers")
    assert get_tool("fetch_top_movers").cache_key("market", {"_query": "Top LOSERS"}) == {"direction": "losers"}


def test_different_screens_get_different_keys_and_phrasing_noise_does_not():
    a = _key_for("screener_parse_query", {"_query": "IT stocks with PE below 20"})
    b = _key_for("screener_parse_query", {"_query": "banks with ROE above 15"})
    a2 = _key_for("screener_parse_query", {"_query": "  it STOCKS with pe   below 20 "})
    assert a != b
    assert a == a2


def test_screener_run_keys_on_parsed_filters():
    k1 = _key_for("screener_run", {"screener_parse_query": {"pe_max": 20}})
    k2 = _key_for("screener_run", {"screener_parse_query": {"roe_min": 15}})
    assert k1 != k2


def test_cacheable_tool_passes_its_key_parts_to_get_and_put():
    spec = dataclasses.replace(get_tool("fetch_top_movers"), fn=MagicMock(return_value=[{"symbol": "X"}]))
    task = {"id": "m", "tool": "fetch_top_movers", "entity": "market", "description": "", "depends_on": [], "status": "pending"}
    with (
        patch("graph.nodes.execute_task.get_tool", return_value=spec),
        patch("graph.nodes.execute_task.evidence_cache.get", return_value=None) as get,
        patch("graph.nodes.execute_task.evidence_cache.put") as put,
    ):
        execute_task(task, {"_query": "top losers"})
    assert get.call_args.kwargs == {"direction": "losers"}
    assert put.call_args.kwargs == {"direction": "losers"}


def test_key_version_is_part_of_the_hash():
    with patch.object(evidence_cache, "_KEY_VERSION", 1):
        old = evidence_cache.compute_args_hash("TCS")
    assert evidence_cache.compute_args_hash("TCS") != old


def test_failed_cache_write_does_not_fail_a_successful_task():
    spec = dataclasses.replace(get_tool("fetch_info"), fn=MagicMock(return_value={"price": 1}))
    task = {"id": "fetch_info:TCS", "tool": "fetch_info", "entity": "TCS", "description": "", "depends_on": [], "status": "pending"}
    with (
        patch("graph.nodes.execute_task.get_tool", return_value=spec),
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", side_effect=RuntimeError("db down")),
    ):
        out = execute_task_node({"task": task, "context": {}})
    assert out["tasks"][0]["status"] == "done"
    assert out["evidence"][0]["payload"] == {"price": 1}
