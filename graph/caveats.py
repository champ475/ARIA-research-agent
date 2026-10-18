"""
graph/caveats.py - plain-language notes about checks that did not pass.

The LLM judge's issues are soft (graph/nodes/verification.py): it could not
confirm a claim, which does not mean the number is wrong. The user gets the
answer plus a note naming what was not confirmed, instead of a yes/no stop.
"""

from __future__ import annotations

import re

_MAX_CLAIMS = 3
# Judge issues that talk about the question or its own limits, not about a claim in the answer.
_META = ("objective", "all llm providers")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _restates_question(claim: str, state: dict) -> bool:
    """The judge often names the user's own question as the claim it could not confirm
    (live traces 2026-10-18). Quoting it back reads as if the question was wrong."""
    c = _norm(claim)
    return bool(c) and any(
        o and (c == o or (len(c) >= 12 and (c in o or o in c)))
        for o in (_norm(state.get("objective", "")), _norm(state.get("query", "")))
    )


GENERIC_NOTE = "Could not fully confirm every figure in this answer against the data collected"


def _claims(state: dict) -> list[str]:
    out: list[str] = []
    for issue in state.get("verification_issues") or []:
        if issue.get("severity", "hard") != "soft":
            continue
        claim = str(issue.get("claim") or "").strip().rstrip(".")
        if claim and _restates_question(claim, state):
            claim = ""  # say it generally, below
            if GENERIC_NOTE not in out:
                out.append(GENERIC_NOTE)
        if claim and not any(m in claim.lower() for m in _META) and claim not in out:
            out.append(claim)
    return out


def verification_note(state: dict) -> str:
    """Two short lines at most, or "" when every check passed."""
    lines: list[str] = []
    claims = _claims(state)
    if claims:
        if claims == [GENERIC_NOTE]:
            lines.append(f"⚠️ *{GENERIC_NOTE}.*")
        else:
            specific = [c for c in claims if c != GENERIC_NOTE]
            shown = "; ".join(specific[:_MAX_CLAIMS])
            more = f" (and {len(specific) - _MAX_CLAIMS} more)" if len(specific) > _MAX_CLAIMS else ""
            lines.append(f"⚠️ *Could not fully confirm: {shown}{more}.*")
    if state.get("judge_status") == "unavailable":
        lines.append("⚠️ *The automatic cross-check was unavailable for this answer.*")
    return "".join(f"\n\n{line}" for line in lines)
