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
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from agents.llm_client import LLMClient
from config import GROQ_MODEL_FAST, TOKENS_FAST
from graph.journal import log_decision
from graph.state import GraphState
from utils.helpers import extract_all_entities

log = logging.getLogger(__name__)

_VALID_INTENTS = ["equity", "report", "mf", "screener", "alert", "planning", "chat"]

_SYSTEM = (
    "You classify a user's financial-research question for ARIA, an autonomous "
    "research engine. Respond with ONLY a JSON object, no prose, no markdown fences:\n"
    '{"intent": one of ["equity","report","mf","screener","alert","planning","chat"], '
    '"secondary_intents": a list of any OTHER domains (same allowed values) this message '
    "ALSO names — e.g. \"generate a report on TCS and alert me if it drops 5%\" is "
    'intent="report", secondary_intents=["alert"]; empty list if only one domain applies, '
    '"objective": a one-sentence statement of what the user actually wants answered, '
    '"complexity": one of ["simple","moderate","complex"]}\n'
    '"simple" = no data lookup needed (definitions, greetings, generic advice). '
    '"moderate" = one entity, one kind of analysis. '
    '"complex" = multiple entities, multiple kinds of analysis, or an action plus '
    "analysis (e.g. compare two stocks and set an alert)."
)

_FALLBACK = {"intent": "chat", "secondary_intents": [], "objective": "", "complexity": "simple"}


def _build_prompt(query: str, entities: list[str]) -> str:
    entity_line = f"Detected entities: {', '.join(entities)}" if entities else "Detected entities: none"
    return f"{entity_line}\nUser query: {query}"


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse(raw: str) -> dict:
    try:
        parsed = json.loads(_strip_code_fence(raw))
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
        return {
            "intent": intent,
            "secondary_intents": clean_secondary,
            "objective": parsed.get("objective", _FALLBACK["objective"]),
            "complexity": parsed.get("complexity", _FALLBACK["complexity"]),
        }
    except (json.JSONDecodeError, AttributeError, TypeError):
        log.warning("QueryAnalyzer: failed to parse LLM output as JSON: %r", raw)
        return dict(_FALLBACK)


def build_query_analyzer_node(client: LLMClient | None = None) -> Callable[[GraphState], dict]:
    """Factory so the graph wiring builds one LLMClient per node type (with the
    right system prompt) and tests can inject a stub without touching a real
    provider. `client=None` constructs a real LLMClient(_SYSTEM)."""
    client = client or LLMClient(_SYSTEM)

    def query_analyzer(state: GraphState) -> dict:
        query = state["query"]
        entities = extract_all_entities(query)
        raw = client.ask(
            _build_prompt(query, entities),
            model=GROQ_MODEL_FAST,
            max_tokens=TOKENS_FAST,
            temperature=0.0,
        )
        classification = _parse(raw)
        return {
            "entities": entities,
            "intent": classification["intent"],
            "secondary_intents": classification["secondary_intents"],
            "objective": classification["objective"],
            "complexity": classification["complexity"],
            **log_decision(
                "QueryAnalyzer",
                f"intent={classification['intent']}, complexity={classification['complexity']}",
                f"entities={entities or 'none'}"
                + (f", secondary_intents={classification['secondary_intents']}" if classification["secondary_intents"] else ""),
            ),
        }

    return query_analyzer
