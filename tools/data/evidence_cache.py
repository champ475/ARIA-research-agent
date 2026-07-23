"""
Evidence cache (RFC_AGENTIC_RESEARCH_GRAPH.md Sec8). Extends the existing
tools/data/cache.py Parquet-cache pattern to graph Evidence: keyed by
(tool, args_hash) in Postgres (utils/db.py's evidence_cache table, shared
across users like pdf_cache/cache_meta), with a per-evidence-type TTL
enforced here rather than at the DB level. ResearchExecutor checks this
before calling a tool so a reflection loop that re-plans the same fetch
reuses the answer instead of re-fetching.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.db import evidence_cache_get, evidence_cache_put

# Per RFC Sec8: OHLCV 15 min, fundamentals 1 day, news 1 hr, macro 1 hr.
# Other evidence types get a conservative default rather than an arbitrary guess.
TTL_SECONDS: dict[str, int] = {
    "price_ohlcv": 900,
    "quote_info": 900,
    "technical_indicator": 900,
    "raw_financials": 86400,
    "fundamental_ratio": 86400,
    "news_headline": 3600,
    "macro": 3600,
    "pdf_extract": 604800,
    "risk_flag": 3600,
}
_DEFAULT_TTL_SECONDS = 900


def compute_args_hash(entity: str, **kwargs: Any) -> str:
    """Deterministic hash of everything that identifies a specific fetch —
    same entity + same extra args always produces the same key regardless of
    dict ordering."""
    payload = {"entity": entity, **kwargs}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(tool: str, entity: str, evidence_type: str, **kwargs: Any) -> dict | None:
    """Returns {"payload": ..., "confidence": ...} if a non-stale cache entry
    exists, else None. Never raises on a DB error — a cache miss is always a
    safe fallback to actually calling the tool."""
    key = compute_args_hash(entity, **kwargs)
    try:
        row = evidence_cache_get(tool, key)
    except Exception:
        return None
    if row is None:
        return None
    ttl = TTL_SECONDS.get(evidence_type, _DEFAULT_TTL_SECONDS)
    fetched_at = row["fetched_at"]
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - fetched_at
    if age > timedelta(seconds=ttl):
        return None
    return {"payload": row["payload"], "confidence": row["confidence"]}


def put(tool: str, entity: str, evidence_type: str, payload: dict, confidence: float = 1.0, **kwargs: Any) -> None:
    key = compute_args_hash(entity, **kwargs)
    evidence_cache_put(tool, key, entity, evidence_type, payload, confidence)
