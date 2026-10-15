"""
utils/financial_profile.py — what may be saved in a financial profile (VAL-05).

The planning intake LLM proposes field updates; nothing reaches
`financial_profiles` until `validate_updates` has checked each field on its
own: age 18–100, capital ≥ 0 rupees (Indian amounts like "5 lakh" parsed),
horizon 1–60 years, a known risk tolerance, bounded free text. Invalid or
implausible values are returned as `rejected` with a reason so the intake
reply asks about them instead of saving them. A wrong profile produces a
confidently wrong allocation, so "lots", "-5" and "500 years" never land.

`wants_build` decides whether the user explicitly asked for the plan, with
word-boundary patterns ("already" no longer contains "ready").
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from utils.numbers import parse_amount

REQUIRED_FIELDS = ("age", "capital", "horizon_years", "goal", "risk_tolerance")

_RISK_SYNONYMS = {
    "conservative": "conservative",
    "low": "conservative",
    "safe": "conservative",
    "cautious": "conservative",
    "moderate": "moderate",
    "medium": "moderate",
    "balanced": "moderate",
    "aggressive": "aggressive",
    "high": "aggressive",
    "very high": "aggressive",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_amount(str(value)) if value is not None else None


class FinancialProfile(BaseModel):
    """Every field optional: a profile fills in over several turns."""

    model_config = ConfigDict(extra="ignore")

    age: int | None = Field(default=None, ge=18, le=100)
    capital: float | None = Field(default=None, ge=0, le=1e12)  # INR lump sum
    horizon_years: int | None = Field(default=None, ge=1, le=60)
    goal: str | None = Field(default=None, min_length=2, max_length=200)
    risk_tolerance: Literal["conservative", "moderate", "aggressive"] | None = None
    monthly_sip: float | None = Field(default=None, ge=0, le=1e9)
    dependents: str | None = Field(default=None, max_length=200)
    income_stability: Literal["salaried", "business", "freelance", "other"] | None = None
    existing_investments: str | None = Field(default=None, max_length=500)
    existing_debts: str | None = Field(default=None, max_length=300)
    tax_regime: Literal["old", "new"] | None = None
    insurance_cover: str | None = Field(default=None, max_length=200)
    emergency_fund: str | None = Field(default=None, max_length=200)
    liquidity_needs: str | None = Field(default=None, max_length=300)
    behavioral_notes: str | None = Field(default=None, max_length=500)

    @field_validator("age", "horizon_years", mode="before")
    @classmethod
    def _whole_number(cls, v: Any) -> Any:
        if v is None:
            return v
        n = _number(v)
        if n is None:
            raise ValueError("needs a number")
        if n != int(n):
            raise ValueError("needs a whole number")
        return int(n)

    @field_validator("capital", "monthly_sip", mode="before")
    @classmethod
    def _rupees(cls, v: Any) -> Any:
        if v is None:
            return v
        n = _number(v)
        if n is None:
            raise ValueError("needs a rupee amount, like 5,00,000 or 5 lakh")
        return n

    @field_validator("risk_tolerance", mode="before")
    @classmethod
    def _risk(cls, v: Any) -> Any:
        if v is None:
            return v
        key = str(v).strip().lower()
        if key not in _RISK_SYNONYMS:
            raise ValueError("must be conservative, moderate or aggressive")
        return _RISK_SYNONYMS[key]

    @field_validator("income_stability", "tax_regime", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return str(v).strip().lower() if v is not None else v

    @field_validator(
        "goal",
        "dependents",
        "existing_investments",
        "existing_debts",
        "insurance_cover",
        "emergency_fund",
        "liquidity_needs",
        "behavioral_notes",
        mode="before",
    )
    @classmethod
    def _text(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        return " ".join(str(v).split())


_LABELS = {
    "age": "age",
    "capital": "investable amount",
    "horizon_years": "time horizon",
    "goal": "goal",
    "risk_tolerance": "risk tolerance",
    "monthly_sip": "monthly SIP",
}


def validate_updates(updates: dict) -> tuple[dict, dict[str, str]]:
    """(clean, rejected): each proposed field validated on its own, so one
    bad value never blocks the good ones. Unknown fields are dropped."""
    clean: dict = {}
    rejected: dict[str, str] = {}
    if not isinstance(updates, dict):
        return clean, rejected
    for key, value in updates.items():
        if key not in FinancialProfile.model_fields or value in (None, ""):
            continue
        try:
            checked = FinancialProfile.model_validate({key: value})
        except ValidationError as e:
            rejected[key] = e.errors()[0]["msg"].removeprefix("Value error, ")
            continue
        clean[key] = getattr(checked, key)
    return clean, rejected


def sanitize_profile(profile: dict) -> dict:
    """A stored profile with any invalid legacy values dropped (rows saved
    before VAL-05 may hold "lots" or -5)."""
    clean, _ = validate_updates(profile or {})
    return clean


def is_complete(profile: dict) -> bool:
    return all(profile.get(f) not in (None, "") for f in REQUIRED_FIELDS)


def rejected_note(rejected: dict[str, str], updates: dict) -> str:
    if not rejected:
        return ""
    parts = [
        f"{_LABELS.get(k, k.replace('_', ' '))} (“{updates.get(k)}”: {why})" for k, why in rejected.items()
    ]
    return "\n\nI didn't save " + "; ".join(parts) + " — could you tell me that again?"


def profile_summary(profile: dict) -> str:
    rows = []
    for key in FinancialProfile.model_fields:
        value = profile.get(key)
        if value in (None, ""):
            continue
        if key in ("capital", "monthly_sip") and isinstance(value, (int, float)):
            from utils.numbers import format_inr

            value = format_inr(value)
        elif key == "horizon_years":
            value = f"{value} years"
        rows.append(f"| {_LABELS.get(key, key.replace('_', ' ')).capitalize()} | {value} |")
    return "| Field | Value |\n|---|---|\n" + "\n".join(rows)


_BUILD_PATTERNS = [
    r"\b(build|generate|create|make|draft|prepare|show)\b[\w\s']{0,20}\b(plan|strategy|allocation|portfolio)\b",
    r"\bgo\s+ahead\b",
    r"\byes\b[,!.\s]*(please\s+)?(build|generate|do\s+it|go\s+ahead|proceed)\b",
    r"\blet\s*'?\s*s\s+(go|do\s+it|build)\b",
    r"\bproceed\b",
]
_BUILD_RE = re.compile("|".join(_BUILD_PATTERNS), re.IGNORECASE)
_NEGATED = re.compile(r"\b(don'?t|do\s+not|not\s+yet|no\s+need|wait|hold\s+on|before\s+you)\b", re.IGNORECASE)


def wants_build(message: str) -> bool:
    """Explicit request to build the plan now. Word-boundary patterns only,
    and a negation ("don't build it yet") wins."""
    text = message or ""
    return bool(_BUILD_RE.search(text)) and not _NEGATED.search(text)
