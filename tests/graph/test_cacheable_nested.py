"""TEST-04 regression: fetch_financials returns {statement: DataFrame} with
Timestamp columns; the checkpointer must be able to store it, and the
ratios computed from the restored copy must match."""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from graph.nodes.execute_task import _to_cacheable, from_cacheable
from tools.analysis.fundamental import compute
from tools.data.providers.fake import FakeProvider


def test_nested_frames_survive_the_checkpointer():
    fin = FakeProvider().financials("INFY")
    stored = _to_cacheable({"statements": fin, "rows": [fin["income_annual"]]})
    kind, blob = JsonPlusSerializer(pickle_fallback=False).dumps_typed(stored)
    restored = from_cacheable(JsonPlusSerializer(pickle_fallback=False).loads_typed((kind, blob)))
    info = FakeProvider().quote("INFY")
    before, after = compute(info, fin), compute(info, restored["statements"])
    for key in ("pe", "pb", "roe", "roce", "de", "net_margin", "current_ratio"):
        assert after[key] == before[key], key
    assert restored["rows"][0].shape == fin["income_annual"].shape


def test_numpy_scalars_and_tool_outputs_are_checkpoint_safe():
    import numpy as np

    from tools.analysis.technical import compute_all, snapshot

    snap = snapshot(compute_all(FakeProvider().ohlcv("TCS")))
    stored = _to_cacheable({"snap": snap, "x": np.float64(1.5), "n": np.int64(3), "t": (np.float32(2.0),)})
    JsonPlusSerializer(pickle_fallback=False).dumps_typed(stored)
    assert stored["x"] == 1.5 and type(stored["n"]) is int


def test_a_full_turn_checkpoints_with_the_strict_serializer(monkeypatch):
    """TEST-04 regression: every state write of a real equity/report turn
    (fake LLM, offline data) must pass the production serializer."""
    from unittest.mock import patch

    from langgraph.checkpoint.memory import InMemorySaver

    from graph.checkpointer import make_serializer
    from graph.graph import build_graph
    from tools.data import providers

    monkeypatch.setattr("config.ARIA_FAKE_LLM", True)
    monkeypatch.setattr("config.ARIA_FAKE_DATA", True)
    providers.set_provider(FakeProvider())
    try:
        graph = build_graph(checkpointer=InMemorySaver(serde=make_serializer()))
        with (
            patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
            patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
        ):
            for q in ("Analyse TCS", "IC report for TCS"):
                out = graph.invoke({"query": q, "max_loops": 2}, config={"configurable": {"thread_id": q}})
                assert out["intent"] in ("equity", "report") and out["tasks"]
    finally:
        providers.set_provider(None)
