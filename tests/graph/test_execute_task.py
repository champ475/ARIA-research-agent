from unittest.mock import MagicMock, patch

import pandas as pd

from graph.nodes.execute_task import execute_task, _to_cacheable, _from_cacheable
from graph.tool_catalogue import ToolSpec


def _task(**overrides):
    base = {
        "id": "fetch_info:RELIANCE",
        "description": "Fetch info",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
    base.update(overrides)
    return base


class TestDataFrameSerialization:
    def test_dataframe_round_trips_through_cacheable_form(self):
        df = pd.DataFrame({"close": [100, 101, 102]})
        cacheable = _to_cacheable(df)
        assert cacheable["__dataframe__"] is True
        restored = _from_cacheable(cacheable)
        assert list(restored["close"]) == [100, 101, 102]

    def test_dict_payload_passes_through_unchanged(self):
        payload = {"pe": 24.3}
        assert _to_cacheable(payload) == payload
        assert _from_cacheable(payload) == payload


class TestExecuteTask:
    def test_cache_hit_skips_tool_call(self):
        fake_fn = MagicMock()
        fake_spec = ToolSpec(
            name="fetch_info", evidence_type="quote_info", depends_on=(),
            cost="low", fn=fake_fn, description="x",
        )
        with patch("graph.nodes.execute_task.get_tool", return_value=fake_spec), \
             patch("graph.nodes.execute_task.evidence_cache.get", return_value={"payload": {"pe": 24.3}, "confidence": 0.8}), \
             patch("graph.nodes.execute_task.evidence_cache.put") as mock_put:
            evidence = execute_task(_task(), context={})

        fake_fn.assert_not_called()
        mock_put.assert_not_called()
        assert evidence["payload"] == {"pe": 24.3}
        assert evidence["confidence"] == 0.8

    def test_cache_miss_calls_tool_and_populates_cache(self):
        fake_fn = MagicMock(return_value={"pe": 30.0})
        fake_spec = ToolSpec(
            name="fetch_info", evidence_type="quote_info", depends_on=(),
            cost="low", fn=fake_fn, description="x",
        )
        with patch("graph.nodes.execute_task.get_tool", return_value=fake_spec), \
             patch("graph.nodes.execute_task.evidence_cache.get", return_value=None), \
             patch("graph.nodes.execute_task.evidence_cache.put") as mock_put:
            evidence = execute_task(_task(), context={"some": "context"})

        fake_fn.assert_called_once_with("RELIANCE", {"some": "context"})
        mock_put.assert_called_once()
        assert evidence["payload"] == {"pe": 30.0}
        assert evidence["confidence"] == 1.0

    def test_evidence_fields_match_task_and_spec(self):
        fake_spec = ToolSpec(
            name="fetch_info", evidence_type="quote_info", depends_on=(),
            cost="low", fn=lambda entity, ctx: {"x": 1}, description="x",
        )
        with patch("graph.nodes.execute_task.get_tool", return_value=fake_spec), \
             patch("graph.nodes.execute_task.evidence_cache.get", return_value=None), \
             patch("graph.nodes.execute_task.evidence_cache.put"):
            evidence = execute_task(_task(), context={})

        assert evidence["tool"] == "fetch_info"
        assert evidence["entity"] == "RELIANCE"
        assert evidence["evidence_type"] == "quote_info"
        assert evidence["source"] == "fetch_info"
        assert evidence["id"]

    def test_tool_failure_propagates(self):
        def _raise(entity, ctx):
            raise ValueError("yfinance down")

        fake_spec = ToolSpec(
            name="fetch_info", evidence_type="quote_info", depends_on=(),
            cost="low", fn=_raise, description="x",
        )
        with patch("graph.nodes.execute_task.get_tool", return_value=fake_spec), \
             patch("graph.nodes.execute_task.evidence_cache.get", return_value=None):
            try:
                execute_task(_task(), context={})
                assert False, "expected ValueError to propagate"
            except ValueError:
                pass
