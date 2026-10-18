"""
evals/answers/memdb.py — in-memory stand-ins for the few utils.db calls the
graph makes itself (EVAL-01): alerts, saved reports and downloads, the
planning profile. One store per eval case, so a case can create an alert and
list it on its next turn. Everything else that reaches Postgres is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any


class MemDB:
    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self.reports: list[dict] = []
        self.profile: dict = {}
        self._ids = count(1)

    # alerts (graph/tool_catalogue.py)
    def alert_add(
        self,
        user_id: int,
        symbol: str,
        alert_type: str,
        condition: str,
        threshold: float | None,
        recurring: bool = False,
        cooldown_hours: int = 24,
        direction: str | None = None,
    ) -> int:
        alert_id = next(self._ids)
        self.alerts.append(
            {
                "id": alert_id,
                "user_id": user_id,
                "symbol": symbol.upper(),
                "alert_type": alert_type,
                "condition": condition,
                "threshold": threshold,
                "recurring": recurring,
                "cooldown_hours": cooldown_hours,
                "direction": direction,
                "active": True,
                "created_at": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
                "triggered_at": None,
            }
        )
        return alert_id

    def alert_list(self, user_id: int, active_only: bool = False) -> list[dict]:
        return [dict(a) for a in reversed(self.alerts) if a["active"] or not active_only]

    def _alert(self, alert_id: int) -> dict | None:
        return next((a for a in self.alerts if a["id"] == alert_id), None)

    def alert_remove(self, user_id: int, alert_id: int) -> bool:
        found = self._alert(alert_id)
        if found:
            self.alerts.remove(found)
        return bool(found)

    def alert_pause(self, user_id: int, alert_id: int) -> None:
        if a := self._alert(alert_id):
            a["active"] = False

    def alert_resume(self, user_id: int, alert_id: int) -> None:
        if a := self._alert(alert_id):
            a["active"] = True

    def screener_save(self, user_id: int, name: str, filters: dict) -> None:
        pass

    # reports and downloads (graph/nodes/report_management.py, output_files.py)
    def report_save(self, user_id: int, symbol: str, report_type: str, title: str, content: str) -> int:
        report_id = next(self._ids)
        self.reports.append(
            {
                "id": report_id,
                "symbol": symbol,
                "report_type": report_type,
                "title": title,
                "content": content,
                "created_at": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
            }
        )
        return report_id

    def report_list(self, user_id: int, limit: int = 20) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "content"} for r in reversed(self.reports)][:limit]

    def report_get(self, user_id: int, report_id: int) -> dict | None:
        return next((dict(r) for r in self.reports if r["id"] == report_id), None)

    def download_put(self, user_id: int, filename: str, content_type: str, content: bytes) -> str:
        return f"eval{next(self._ids):04d}"

    # planning (graph/nodes/planning_node.py)
    def profile_get(self, user_id: int) -> dict:
        return dict(self.profile)

    def profile_upsert(self, user_id: int, updates: dict) -> dict:
        self.profile.update(updates)
        return dict(self.profile)

    def profile_clear(self, user_id: int) -> None:
        self.profile = {}


# module attribute -> MemDB method
TARGETS: dict[str, str] = {
    "graph.tool_catalogue.db_alert_add": "alert_add",
    "graph.tool_catalogue.db_alert_list": "alert_list",
    "graph.tool_catalogue.db_alert_remove": "alert_remove",
    "graph.tool_catalogue.db_alert_pause": "alert_pause",
    "graph.tool_catalogue.db_alert_resume": "alert_resume",
    "graph.tool_catalogue.db_screener_save": "screener_save",
    "graph.nodes.report_management.report_list": "report_list",
    "graph.nodes.report_management.report_get": "report_get",
    "graph.nodes.output_files.report_save": "report_save",
    "graph.nodes.output_files.download_put": "download_put",
    "graph.nodes.planning_node.profile_get": "profile_get",
    "graph.nodes.planning_node.profile_upsert": "profile_upsert",
    "graph.nodes.planning_node.profile_clear": "profile_clear",
}


class Router:
    """Forwards each patched name to the current case's MemDB."""

    def __init__(self) -> None:
        self.db = MemDB()

    def reset(self) -> None:
        self.db = MemDB()

    def method(self, name: str) -> Any:
        return lambda *args, **kwargs: getattr(self.db, name)(*args, **kwargs)
