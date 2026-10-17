"""
graph/caveats.py - plain-language notes about checks that did not pass.

The LLM judge's issues are soft (graph/nodes/verification.py): it could not
confirm a claim, which does not mean the number is wrong. The user gets the
answer plus a note naming what was not confirmed, instead of a yes/no stop.
"""

from __future__ import annotations

_MAX_CLAIMS = 3
# Judge issues that talk about the question or its own limits, not about a claim in the answer.
_META = ("objective", "all llm providers")


def _claims(state: dict) -> list[str]:
    out: list[str] = []
    for issue in state.get("verification_issues") or []:
        if issue.get("severity", "hard") != "soft":
            continue
        claim = str(issue.get("claim") or "").strip().rstrip(".")
        if claim and not any(m in claim.lower() for m in _META) and claim not in out:
            out.append(claim)
    return out


def verification_note(state: dict) -> str:
    """Two short lines at most, or "" when every check passed."""
    lines: list[str] = []
    claims = _claims(state)
    if claims:
        shown = "; ".join(claims[:_MAX_CLAIMS])
        more = f" (and {len(claims) - _MAX_CLAIMS} more)" if len(claims) > _MAX_CLAIMS else ""
        lines.append(f"⚠️ *Could not fully confirm: {shown}{more}.*")
    if state.get("judge_status") == "unavailable":
        lines.append("⚠️ *The automatic cross-check was unavailable for this answer.*")
    return "".join(f"\n\n{line}" for line in lines)
