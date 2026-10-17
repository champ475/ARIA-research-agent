"""
graph/schemas.py — the JSON each LLM-backed node must return (LLM-05).

Lenient by design: unknown fields are ignored and optional fields default, so
a reply is rejected (and repaired once) only when it is genuinely unusable —
not JSON at all, or a field of the wrong shape. Each node still applies its
own business rules (valid intents, known tools) after validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _list_or_empty(v: Any) -> Any:
    return v if isinstance(v, list) else []


class QueryAnalysis(_Lenient):
    intent: str = "chat"
    secondary_intents: list[Any] = []
    objective: str = ""
    complexity: str = "simple"
    companies: list[Any] = []

    _lists = field_validator("secondary_intents", "companies", mode="before")(_list_or_empty)


class ToolSelection(_Lenient):
    tools: list[str] = []


class ReflectionResult(_Lenient):
    missing: list[dict[str, Any]] = []


class VerificationResult(_Lenient):
    issues: list[dict[str, Any]] = []
