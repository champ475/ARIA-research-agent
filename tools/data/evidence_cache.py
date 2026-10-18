"""
Evidence cache (RFC_AGENTIC_RESEARCH_GRAPH.md Sec8). Extends the existing
tools/data/cache.py Parquet-cache pattern to graph Evidence: keyed by
(tool, args_hash) in Postgres (utils/db.py's evidence_cache table, shared
across users like pdf_cache), with a per-evidence-type TTL
enforced here rather than at the DB level. ResearchExecutor checks this
before calling a tool so a reflection loop that re-plans the same fetch
reuses the answer instead of re-fetching.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.db import evidence_cache_get, evidence_cache_put

log = logging.getLogger(__name__)

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
    "market_overview": 900,
    "top_movers": 900,
}
_DEFAULT_TTL_SECONDS = 900

# Bumped whenever the key scheme changes, so rows written under an old (and
# possibly under-specified) scheme can never be served again. v2 = DATA-01:
# query-driven tools started adding their parsed inputs to the key.
_KEY_VERSION = 4  # v4: calculation details lost their doubled rupee sign (v3: statement currency, lender rows, fund names)


def compute_args_hash(entity: str, **kwargs: Any) -> str:
    """Deterministic hash of everything that identifies a specific fetch —
    same entity + same extra args always produces the same key regardless of
    dict ordering."""
    payload = {"_v": _KEY_VERSION, "entity": entity, **kwargs}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(tool: str, entity: str, evidence_type: str, **kwargs: Any) -> dict | None:
    """Returns {"payload", "confidence", "fetched_at"} if a non-stale cache entry
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
    return {"payload": row["payload"], "confidence": row["confidence"], "fetched_at": fetched_at}


def put(tool: str, entity: str, evidence_type: str, payload: dict, confidence: float = 1.0, **kwargs: Any) -> None:
    """Best-effort, like get(): a failed cache write must never turn a tool
    call that already succeeded into a failed task."""
    key = compute_args_hash(entity, **kwargs)
    try:
        evidence_cache_put(tool, key, entity, evidence_type, payload, confidence)
    except Exception as e:
        log.warning(f"[evidence_cache] put failed for {tool}: {e}")
