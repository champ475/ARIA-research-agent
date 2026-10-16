"""OBS-03 on a real Postgres: the SLO views and the weekly report agree with
a hand count of seeded turns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration


def _turn(db, status, intent="equity", ttft_ms=5000, days_ago=0):
    tid = str(uuid.uuid4())
    db.turn_record(
        turn_id=tid,
        user_id=None,
        session_id=None,
        intent=intent,
        complexity="moderate",
        status=status,
        error_id=tid.replace("-", "")[:8],
        latency_ms=ttft_ms + 1000,
        ttft_ms=ttft_ms,
        llm_calls=2,
        tokens_in=100,
        tokens_out=50,
        tokens_estimated=False,
        providers=["groq"],
        prompt_versions={},
        tool_calls=3,
        cache_hits=1,
        node_ms={},
        data_gaps=0,
        grounding=None,
    )
    if days_ago:
        db._query(
            "UPDATE turns SET created_at=%s WHERE turn_id=%s",
            (datetime.now(UTC) - timedelta(days=days_ago), tid),
            fetch=None,
            commit=True,
        )


def test_slo_report_matches_a_hand_count(db):
    import scripts.slo_report as slo

    for _ in range(196):
        _turn(db, "ok", ttft_ms=4000)
    for status in ("error", "llm_unavailable", "refused", "quota"):  # 2 failures, 2 deliberate answers
        _turn(db, status)
    for _ in range(12):
        _turn(db, "ok", ttft_ms=40_000)  # slow equity answers: > 5% of the week
    _turn(db, "error", days_ago=20)  # in the month, not the week

    r = slo.report()
    week, month = r["week"], r["month"]
    assert (week["turns"], week["failed"]) == (212, 2)
    assert week["availability"] == round(210 / 212, 5) and week["availability_ok"] is False  # < 99.5%
    assert week["budget_used"] == round(2 / (0.005 * 212), 3)
    assert (month["turns"], month["failed"]) == (213, 3)
    assert week["equity_p95_ttft_s"] == 40.0 and week["latency_ok"] is False
    assert week["burning_fast"] is True  # 2 failures > 7/30 of a ~1-failure monthly budget
    stats = db._query("SELECT sum(turns) AS t, sum(failed) AS f FROM turn_stats_hourly", fetch="one")
    assert (int(stats["t"]), int(stats["f"])) == (213, 3)
    daily = db._query("SELECT sum(turns) AS t, sum(good) AS g FROM slo_daily", fetch="one")
    assert (int(daily["t"]), int(daily["g"])) == (213, 210)
