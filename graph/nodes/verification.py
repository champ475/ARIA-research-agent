"""
Verification node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6): a cascade, cheapest
check first, short-circuiting on the first stage that finds anything. Stage 3
(LLM judge) only runs when stages 1-2 found nothing — the concrete cost
mitigation from RFC Sec17: most turns should never reach an LLM call inside
Verification at all.

1. Deterministic — evidence schema validation, every done task's cited
   evidence actually exists in state, numeric sanity bounds on fundamental
   ratios (NaN / absurd magnitude).
2. Semantic — confidence-floor check, contradiction detection between two
   evidence entries for the same (entity, evidence_type) whose overlapping
   numeric fields disagree beyond a tolerance.
3. LLM judge — asks whether the evidence actually supports the objective;
   only reached when 1-2 are clean.

Any stage that finds issues increments loop_count by 1, unconditionally —
this is the same shared budget Reflection increments when it queues new
tasks. Without Verification also incrementing it, a Reflection<->Verification
ping-pong (Verification flags something Reflection's own lens never
independently reports as "missing") would never terminate, since Reflection
alone only advances the counter when it decides to add work.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from agents.llm_client import LLMClient, LLMProvidersExhausted
from config import EVIDENCE_PROMPT_CHAR_BUDGET, GROQ_MODEL_FAST, TOKENS_AGENT
from graph.deadline import past_research_cutoff
from graph.journal import log_decision
from graph.nodes.evidence_summarizer import cap_evidence_for_prompt
from graph.prompt_safety import UNTRUSTED_NOTICE, wrap_untrusted
from graph.schemas import VerificationResult
from graph.state import Evidence, GraphState, VerificationIssue
from utils.llm_json import ParseFailure, extract_json, json_reply

log = logging.getLogger(__name__)

_REQUIRED_EVIDENCE_FIELDS = (
    "id",
    "source",
    "tool",
    "entity",
    "evidence_type",
    "payload",
    "timestamp",
    "confidence",
    "citation_key",
)
_CONFIDENCE_FLOOR = 0.5
_CONTRADICTION_TOLERANCE = 0.05  # 5% relative difference
_VALID_ISSUE_TYPES = {"unsupported", "contradiction", "low_confidence", "missing_citation"}

_SYSTEM = (
    "You judge whether collected evidence actually supports answering a "
    "financial-research objective for ARIA. Respond with ONLY a JSON object, "
    'no prose, no markdown fences: {"issues": [{"claim": "...", '
    '"issue_type": "unsupported|contradiction|low_confidence|missing_citation", '
    '"evidence_ids": ["..."]}]}. An empty issues list means the evidence is '
    "sufficient and consistent. Be conservative — only flag a genuine "
    "problem, not a stylistic preference." + UNTRUSTED_NOTICE
)


def _numeric_fields(payload) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    return {
        k: float(v) for k, v in payload.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _is_missing(value) -> bool:
    """True if a required Evidence field is absent. Deliberately avoids `value
    in (None, "")` — some payloads (fetch_ohlcv, fetch_nav, fetch_category_funds)
    are pandas DataFrames, and DataFrame.__eq__ returns an elementwise frame
    rather than a bool, which raises ValueError the moment Python's `in`
    operator tries to short-circuit on it. `is None` is identity-safe for any
    type; the empty-string check only applies to actual strings."""
    if value is None:
        return True
    return isinstance(value, str) and value == ""


def _stage1_deterministic(evidence: list[Evidence], tasks: list[dict]) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []

    for ev in evidence:
        missing = [f for f in _REQUIRED_EVIDENCE_FIELDS if _is_missing(ev.get(f))]
        if missing:
            issues.append(
                {
                    "claim": f"evidence {ev.get('id', '?')} missing fields: {missing}",
                    "issue_type": "missing_citation",
                    "stage": "deterministic",
                    "evidence_ids": [ev.get("id", "?")],
                }
            )

    evidence_ids = {ev["id"] for ev in evidence}
    for task in tasks:
        if task.get("status") != "done":
            continue
        for eid in task.get("result_evidence_ids", []):
            if eid not in evidence_ids:
                issues.append(
                    {
                        "claim": f"task {task['id']} references missing evidence {eid}",
                        "issue_type": "unsupported",
                        "stage": "deterministic",
                        "evidence_ids": [eid],
                    }
                )

    for ev in evidence:
        if ev.get("evidence_type") != "fundamental_ratio":
            continue
        for field, value in _numeric_fields(ev["payload"]).items():
            if value != value:  # NaN
                issues.append(
                    {
                        "claim": f"{ev['entity']} {field} is NaN",
                        "issue_type": "unsupported",
                        "stage": "deterministic",
                        "evidence_ids": [ev["id"]],
                    }
                )
            elif abs(value) > 1e6:
                issues.append(
                    {
                        "claim": f"{ev['entity']} {field}={value} is out of a sane range",
                        "issue_type": "unsupported",
                        "stage": "deterministic",
                        "evidence_ids": [ev["id"]],
                    }
                )
    return issues


def _stage2_semantic(evidence: list[Evidence]) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []

    for ev in evidence:
        if ev["confidence"] < _CONFIDENCE_FLOOR:
            issues.append(
                {
                    "claim": f"{ev['entity']} {ev['evidence_type']} has low confidence ({ev['confidence']})",
                    "issue_type": "low_confidence",
                    "stage": "semantic",
                    "evidence_ids": [ev["id"]],
                }
            )

    groups: dict[tuple[str, str], list[Evidence]] = {}
    for ev in evidence:
        groups.setdefault((ev["entity"], ev["evidence_type"]), []).append(ev)

    for (entity, evidence_type), group in groups.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = _numeric_fields(group[i]["payload"]), _numeric_fields(group[j]["payload"])
                for field in set(a) & set(b):
                    va, vb = a[field], b[field]
                    denom = max(abs(va), abs(vb), 1e-9)
                    if abs(va - vb) / denom > _CONTRADICTION_TOLERANCE:
                        issues.append(
                            {
                                "claim": f"{entity} {evidence_type}.{field} disagrees: {va} vs {vb}",
                                "issue_type": "contradiction",
                                "stage": "semantic",
                                "evidence_ids": [group[i]["id"], group[j]["id"]],
                            }
                        )
    return issues


def _parse_llm_issues(raw: str) -> list[VerificationIssue]:
    try:
        parsed = extract_json(raw)
        raw_issues = parsed.get("issues", [])
    except (ParseFailure, AttributeError, TypeError):
        log.warning("Verification: failed to parse LLM judge output: %r", raw)
        return []

    issues: list[VerificationIssue] = []
    for entry in raw_issues:
        if not isinstance(entry, dict) or "claim" not in entry:
            continue
        issue_type = entry.get("issue_type", "unsupported")
        if issue_type not in _VALID_ISSUE_TYPES:
            issue_type = "unsupported"
        issues.append(
            {
                "claim": entry["claim"],
                "issue_type": issue_type,
                "stage": "llm_judge",
                "evidence_ids": entry.get("evidence_ids", []),
            }
        )
    return issues


def _build_judge_prompt(objective: str, evidence_summary: dict) -> str:
    capped = cap_evidence_for_prompt(evidence_summary, EVIDENCE_PROMPT_CHAR_BUDGET)
    evidence_json = json.dumps(capped, default=str)
    return f"Objective: {objective}\n\nEvidence summary:\n{wrap_untrusted('evidence', evidence_json)}"


def build_verification_node(
    client: LLMClient | None = None,
    provider_config: dict | None = None,
) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM, provider_config)

    def verification(state: GraphState) -> dict:
        evidence = state.get("evidence", [])
        tasks = state.get("tasks", [])
        if past_research_cutoff(state):
            # Another loop can't fit; the answer names its data gaps instead.
            return {
                "verification_issues": [],
                **log_decision("Verification", "skipped", "turn deadline reached — no further loop"),
            }

        stage1_issues = _stage1_deterministic(evidence, tasks)
        if stage1_issues:
            return {
                "verification_issues": stage1_issues,
                "loop_count": 1,
                **log_decision(
                    "Verification",
                    f"{len(stage1_issues)} issue(s) at deterministic stage",
                    "schema/citation/numeric-range checks failed — LLM judge skipped",
                ),
            }

        stage2_issues = _stage2_semantic(evidence)
        if stage2_issues:
            return {
                "verification_issues": stage2_issues,
                "loop_count": 1,
                **log_decision(
                    "Verification",
                    f"{len(stage2_issues)} issue(s) at semantic stage",
                    "contradiction or confidence-floor check failed — LLM judge skipped",
                ),
            }

        max_loops = state.get("max_loops", 2)
        try:
            raw = json_reply(
                client,
                VerificationResult,
                "verification",
                _build_judge_prompt(state.get("objective", ""), state.get("evidence_summary", {})),
                model=GROQ_MODEL_FAST,
                max_tokens=TOKENS_AGENT,
                temperature=0.0,
            )
        except LLMProvidersExhausted as e:
            # Honest terminal state instead of silently looking clean — an
            # empty issues list here would be indistinguishable from "the
            # judge actually found nothing wrong". loop_count forced to
            # max_loops (reducer is Annotated[int, operator.add], see
            # graph/state.py) so this doesn't trigger another retry cycle.
            log.warning(f"Verification: all LLM providers exhausted, treating as terminal — {e}")
            return {
                "verification_issues": [
                    {
                        "claim": "Could not verify — all LLM providers were unavailable",
                        "issue_type": "unsupported",
                        "stage": "llm_judge",
                        "evidence_ids": [],
                    }
                ],
                "loop_count": max_loops,
                **log_decision(
                    "Verification", "llm_judge skipped — all providers exhausted", type(e).__name__
                ),
            }
        stage3_issues = _parse_llm_issues(raw)
        # loop_count is a shared budget incremented by whichever side (Reflection
        # or Verification) is asking for another pass — this is what keeps a
        # Reflection<->Verification ping-pong (Verification flags something
        # Reflection's own lens never independently notices as "missing")
        # finite even though Reflection itself only increments when IT decides
        # to queue new tasks.
        extra = {"loop_count": 1} if stage3_issues else {}
        return {
            "verification_issues": stage3_issues,
            **extra,
            **log_decision(
                "Verification",
                f"{len(stage3_issues)} issue(s) at llm_judge stage"
                if stage3_issues
                else "clean, all stages passed",
                "deterministic and semantic stages passed, escalated to LLM judgment",
            ),
        }

    return verification
