"""
QueryAnalyzer node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). Cheap classification
call — entity extraction is fully deterministic (reused from utils.helpers,
which already does alias resolution like SBI -> SBIN); only intent/objective/
complexity go through the LLM, routed via GROQ_MODEL_FAST since this runs on
every turn and doesn't need the primary model's reasoning depth.

Phase 7.4 (multi-intent-in-one-turn): `secondary_intents` lets one message
name more than one domain (e.g. "generate a report on TCS and alert me if it
drops 5%" -> intent="report", secondary_intents=["alert"]). Planner itself
needed NO change to support this — it already free-selects tools from the
WHOLE catalogue based on `objective` text, never gated by `intent` at all
(intent is only consumed downstream, by CommunicationLayer's domain-specific
branches and by GraphOrchestrator's tab_used label). This field only exists
so those two consumers know a second domain was named, not to change what
tools Planner considers.

Entities (VAL-02, graph/entity_resolution.py): company mentions are found
deterministically, plus the mentions this node's LLM call reports as written
by the user (typos included), and every one is resolved against the NSE
instrument master (tools/data/instruments.py) — found / ambiguous /
not_found. Only resolved symbols reach `entities`, so ordinary words never
fan out into tasks or alert rows, and the LLM can never invent a ticker.
Unresolved mentions go to `entity_resolutions` for Planner/CommunicationLayer
to ask about. History supplies the entity only when the message names no
company at all — an unknown name no longer silently becomes the previous
turn's ticker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from agents.llm_client import LLMClient
from config import GROQ_MODEL_FAST, MAX_ENTITIES_PER_TURN, TOKENS_FAST
from graph.data_questions import min_complexity
from graph.entity_resolution import add_llm_mentions, history_entities, resolve_mentions
from graph.journal import log_decision
from graph.state import GraphState
from utils.helpers import strip_code_fence

log = logging.getLogger(__name__)

_VALID_INTENTS = ["equity", "report", "mf", "screener", "alert", "planning", "chat"]

_SYSTEM = (
    "You classify a user's financial-research question for ARIA, an autonomous "
    "research engine. Respond with ONLY a JSON object, no prose, no markdown fences:\n"
    '{"intent": one of ["equity","report","mf","screener","alert","planning","chat"], '
    '"secondary_intents": a list of any OTHER domains (same allowed values) this message '
    'ALSO names — e.g. "generate a report on TCS and alert me if it drops 5%" is '
    'intent="report", secondary_intents=["alert"]; empty list if only one domain applies, '
    '"objective": a one-sentence statement of what the user actually wants answered, '
    '"complexity": one of ["simple","moderate","complex"], '
    '"companies": every company or stock the user names, copied exactly as written '
    "(keep typos; not mutual funds, indices or sectors); empty list if none}\n"
    '"simple" = no data lookup needed (definitions, greetings, generic advice). '
    '"moderate" = one entity, one kind of analysis. '
    '"complex" = multiple entities, multiple kinds of analysis, or an action plus '
    "analysis (e.g. compare two stocks and set an alert)."
)

_FALLBACK = {
    "intent": "chat",
    "secondary_intents": [],
    "objective": "",
    "complexity": "simple",
    "companies": [],
}


_HISTORY_TURNS_IN_PROMPT = 6  # last N history messages included verbatim — keeps this cheap call cheap


def _build_prompt(query: str, entities: list[str], history: list[dict]) -> str:
    entity_line = f"Detected entities: {', '.join(entities)}" if entities else "Detected entities: none"
    lines = []
    if history:
        lines.append("Recent conversation (oldest first):")
        for msg in history[-_HISTORY_TURNS_IN_PROMPT:]:
            lines.append(f"{msg.get('role', 'user')}: {msg.get('content', '')[:300]}")
        lines.append("")
    lines.append(entity_line)
    lines.append(f"User query: {query}")
    return "\n".join(lines)


def _parse(raw: str) -> dict:
    try:
        parsed = json.loads(strip_code_fence(raw))
        intent = parsed.get("intent", _FALLBACK["intent"])
        secondary = parsed.get("secondary_intents") or []
        if not isinstance(secondary, list):
            secondary = []
        # Never duplicate the primary intent, never include an invalid label,
        # dedupe while preserving order.
        seen = {intent}
        clean_secondary = []
        for s in secondary:
            if s in _VALID_INTENTS and s not in seen:
                seen.add(s)
                clean_secondary.append(s)
        companies = parsed.get("companies") or []
        return {
            "intent": intent,
            "secondary_intents": clean_secondary,
            "objective": parsed.get("objective", _FALLBACK["objective"]),
            "complexity": parsed.get("complexity", _FALLBACK["complexity"]),
            "companies": [c for c in companies if isinstance(c, str)] if isinstance(companies, list) else [],
        }
    except (json.JSONDecodeError, AttributeError, TypeError):
        log.warning("QueryAnalyzer: failed to parse LLM output as JSON: %r", raw)
        return dict(_FALLBACK)


def build_query_analyzer_node(
    client: LLMClient | None = None,
    provider_config: dict | None = None,
) -> Callable[[GraphState], dict]:
    """Factory so the graph wiring builds one LLMClient per node type (with the
    right system prompt) and tests can inject a stub without touching a real
    provider. `client=None` constructs a real LLMClient(_SYSTEM, provider_config)."""
    client = client or LLMClient(_SYSTEM, provider_config)

    def query_analyzer(state: GraphState) -> dict:
        query = state["query"]
        history = state.get("history", [])
        resolved = resolve_mentions(query)
        raw = client.ask(
            _build_prompt(query, resolved.entities, history),
            model=GROQ_MODEL_FAST,
            max_tokens=TOKENS_FAST,
            temperature=0.0,
        )
        classification = _parse(raw)
        add_llm_mentions(resolved, classification["companies"])
        entities = resolved.entities if resolved.mentioned else history_entities(history)
        unresolved = list(resolved.unresolved)
        if len(entities) > MAX_ENTITIES_PER_TURN:
            # VAL-03: tasks = companies x tools; past the cap, ask instead of
            # fanning out dozens of fetches (blocking, like an ambiguous name).
            unresolved.append(
                {
                    "query": ", ".join(entities),
                    "status": "too_many",
                    "candidates": [{"symbol": e, "name": e} for e in entities],
                    "limit": MAX_ENTITIES_PER_TURN,
                }
            )
        # LLM-06: a data question never takes the no-research path.
        complexity, raised = min_complexity(query, entities, classification["complexity"])
        return {
            "entities": entities,
            "entity_resolutions": unresolved,
            "entity_notes": resolved.notes,
            "intent": classification["intent"],
            "secondary_intents": classification["secondary_intents"],
            "objective": classification["objective"],
            "complexity": complexity,
            **log_decision(
                "QueryAnalyzer",
                f"intent={classification['intent']}, complexity={complexity}"
                + (f" (raised from simple: {raised})" if raised else ""),
                f"entities={entities or 'none'}"
                + (f", unresolved={[u['query'] for u in resolved.unresolved]}" if resolved.unresolved else "")
                + (
                    f", secondary_intents={classification['secondary_intents']}"
                    if classification["secondary_intents"]
                    else ""
                ),
            ),
        }

    return query_analyzer
