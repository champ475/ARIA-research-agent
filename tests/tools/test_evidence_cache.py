from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tools.data import evidence_cache


class TestArgsHash:
    def test_deterministic_regardless_of_kwarg_order(self):
        h1 = evidence_cache.compute_args_hash("RELIANCE", period="1y", interval="1d")
        h2 = evidence_cache.compute_args_hash("RELIANCE", interval="1d", period="1y")
        assert h1 == h2

    def test_different_entity_produces_different_hash(self):
        h1 = evidence_cache.compute_args_hash("RELIANCE")
        h2 = evidence_cache.compute_args_hash("TCS")
        assert h1 != h2

    def test_different_kwargs_produce_different_hash(self):
        h1 = evidence_cache.compute_args_hash("RELIANCE", period="1y")
        h2 = evidence_cache.compute_args_hash("RELIANCE", period="2y")
        assert h1 != h2


class TestGet:
    def test_returns_none_on_cache_miss(self):
        with patch("tools.data.evidence_cache.evidence_cache_get", return_value=None):
            result = evidence_cache.get("fetch_info", "RELIANCE", "quote_info")
        assert result is None

    def test_returns_payload_when_fresh(self):
        fresh_row = {
            "payload": {"pe": 24.3},
            "confidence": 0.9,
            "fetched_at": datetime.now(UTC) - timedelta(seconds=10),
        }
        with patch("tools.data.evidence_cache.evidence_cache_get", return_value=fresh_row):
            result = evidence_cache.get("fetch_info", "RELIANCE", "quote_info")
        assert result == {"payload": {"pe": 24.3}, "confidence": 0.9, "fetched_at": fresh_row["fetched_at"]}

    def test_returns_none_when_stale(self):
        # quote_info TTL is 900s — this row is older than that.
        stale_row = {
            "payload": {"pe": 24.3},
            "confidence": 0.9,
            "fetched_at": datetime.now(UTC) - timedelta(seconds=1000),
        }
        with patch("tools.data.evidence_cache.evidence_cache_get", return_value=stale_row):
            result = evidence_cache.get("fetch_info", "RELIANCE", "quote_info")
        assert result is None

    def test_respects_per_evidence_type_ttl(self):
        # 2000s old: stale for quote_info (900s TTL) but fresh for raw_financials (86400s TTL).
        row = {
            "payload": {"x": 1},
            "confidence": 1.0,
            "fetched_at": datetime.now(UTC) - timedelta(seconds=2000),
        }
        with patch("tools.data.evidence_cache.evidence_cache_get", return_value=row):
            assert evidence_cache.get("fetch_info", "RELIANCE", "quote_info") is None
            assert evidence_cache.get("fetch_financials", "RELIANCE", "raw_financials") is not None

    def test_handles_naive_datetime_from_db(self):
        # psycopg2 can return naive datetimes depending on column config —
        # must not crash comparing naive vs aware.
        naive_row = {
            "payload": {"x": 1},
            "confidence": 1.0,
            "fetched_at": datetime.utcnow() - timedelta(seconds=10),
        }
        with patch("tools.data.evidence_cache.evidence_cache_get", return_value=naive_row):
            result = evidence_cache.get("fetch_info", "RELIANCE", "quote_info")
        assert result is not None

    def test_db_error_is_a_safe_cache_miss(self):
        with patch(
            "tools.data.evidence_cache.evidence_cache_get", side_effect=Exception("connection refused")
        ):
            result = evidence_cache.get("fetch_info", "RELIANCE", "quote_info")
        assert result is None


class TestPut:
    def test_put_forwards_computed_hash(self):
        with patch("tools.data.evidence_cache.evidence_cache_put") as mock_put:
            evidence_cache.put("fetch_info", "RELIANCE", "quote_info", {"pe": 24.3}, confidence=0.9)
        expected_hash = evidence_cache.compute_args_hash("RELIANCE")
        mock_put.assert_called_once_with(
            "fetch_info", expected_hash, "RELIANCE", "quote_info", {"pe": 24.3}, 0.9
        )
