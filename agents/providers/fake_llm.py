"""
agents/providers/fake_llm.py — a deterministic stand-in for every LLM call
(TEST-04, `ARIA_FAKE_LLM=1`).

End-to-end tests and offline demos must not depend on a cloud model being
up, on quota, or on its wording. This provider answers each graph node from
the prompt with fixed rules: QueryAnalyzer classifies by keywords, the
Planner picks a fixed tool set per intent, Reflection/Verification find
nothing to fix, and the answer node restates the verified conclusions it
was given — so every figure in the answer is in its prompt (LLM-03 holds).
Never enabled in production (config boot check).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from utils import faults

_INTENT_RULES = [
    ("alert", r"\balert|notify me|remind me|\bcancel\b.*\b(alert|#\d+)|pause .*alert|my alerts"),
    (
        "planning",
        r"\bplan\b|retire|risk tolerance|\bgoal\b|i'?m \d{2}\b|\bcapital\b|invest(?:ing)? \d|build my",
    ),
    ("mf", r"\bfunds?\b|\belss\b|mutual|\bsip\b|\bnav\b|flexi ?cap|index fund"),
    ("screener", r"stocks? with|\bscreen|pe (?:below|under|above)|gainers|losers|top movers"),
    ("report", r"\breport\b|initiat(?:e|ing) coverage"),
]
_TOOLS = {
    "equity": ["fetch_info", "compute_fundamentals", "fetch_ohlcv", "compute_technicals", "fetch_news"],
    "report": ["fetch_info", "compute_fundamentals", "fetch_ohlcv"],
    "mf": ["fetch_category_funds"],
    "screener": ["screener_parse_query", "screener_run", "screener_to_markdown"],
}


def _query(prompt: str) -> str:
    m = re.search(r"User query: (.+)", prompt)
    return (m.group(1) if m else prompt).strip()


def classify(query: str, has_company: bool) -> dict[str, Any]:
    q = query.lower()
    intent = next((name for name, rx in _INTENT_RULES if re.search(rx, q)), None)
    if intent is None:
        intent = "equity" if has_company else "chat"
    complexity = "moderate" if intent in ("equity", "report", "mf", "screener") else "simple"
    return {
        "intent": intent,
        "secondary_intents": [],
        "objective": query,
        "complexity": complexity,
        "companies": [],
    }


def _plan(prompt: str) -> list[str]:
    found = re.search(r"Objective: (.+)", prompt)
    objective = found.group(1).lower() if found else ""
    available = set(re.findall(r"^- ([a-z_]+):", prompt, flags=re.M))
    if "alert" in objective:
        if re.search(r"\b(cancel|remove|delete)\b", objective):
            tools = ["alert_cancel"]
        elif re.search(r"\b(show|list|my alerts)\b", objective):
            tools = ["alert_list"]
        else:
            tools = ["alert_create"]
    elif re.search(r"gainers|losers|top movers", objective):
        tools = ["fetch_top_movers"]
    else:
        intent = classify(objective, True)["intent"]
        tools = _TOOLS.get(intent, _TOOLS["equity"])
    return [t for t in tools if t in available] or [t for t in tools if not available]


def _intake(prompt: str) -> dict[str, Any]:
    from utils.numbers import parse_inr_amount

    found = re.search(r'USER\'S LATEST MESSAGE: "(.*)"', prompt, flags=re.S)
    msg = found.group(1) if found else ""
    m = msg.lower()
    updates: dict[str, Any] = {}
    if age := re.search(r"\b(?:i'?m|age|aged)\s*(\d{2})\b|\b(\d{2})\s*(?:years? old|yo)\b", m):
        updates["age"] = int(age.group(1) or age.group(2))
    if amt := re.search(r"(?:₹|rs\.?|inr)?\s*\d[\d,.]*\s*(?:l|lakh|lac|cr|crore|k)\b", m):
        parsed = parse_inr_amount(amt.group(0))
        if parsed is not None:
            updates["capital"] = float(parsed.value)
    if years := re.search(r"\b(\d{1,2})\s*years?\b(?! old)", m):
        updates["horizon_years"] = int(years.group(1))
    for goal in ("retirement", "house", "education", "wealth"):
        if goal in m or (goal == "retirement" and "retire" in m):
            updates["goal"] = goal
    for risk in ("conservative", "moderate", "aggressive"):
        if risk in m:
            updates["risk_tolerance"] = risk
    return {"updates": updates, "reply": None}


def _answer(prompt: str) -> str:
    claims = [ln for ln in prompt.splitlines() if ln.startswith("- ")][:8]
    if not claims:
        claims = [f"- {ln.strip()}" for ln in prompt.splitlines() if ":" in ln and len(ln) < 160][:6]
    body = "\n".join(re.sub(r"\s*\(confidence: [0-9.]+\)", "", c) for c in claims)
    return "Here is what the research shows:\n\n" + (body or "- No verified figures were available for this.")


class FakeLLMProvider:
    name = "fake"
    model = "fake-llm"

    def __init__(self, system_prompt: str = "", node: str | None = None):
        self.system = system_prompt
        self.node = node or "other"
        self.last_usage: tuple[int, int] | None = None

    def _reply(self, prompt: str) -> str:
        node = self.node
        if node == "query_analyzer":
            has_company = "Detected entities: none" not in prompt
            return json.dumps(classify(_query(prompt), has_company))
        if node == "planner":
            return json.dumps({"tools": _plan(prompt)})
        if node == "reflection":
            return json.dumps({"missing": []})
        if node == "verification":
            return json.dumps({"issues": []})
        if node == "planning_intake" or "USER'S LATEST MESSAGE:" in prompt:
            # The intake runs on whichever client planning holds; recognise it by its prompt.
            return json.dumps(_intake(prompt))
        if node == "suggestions":
            return json.dumps(
                ["Show the technicals", "Latest news", "Set a price alert", "Compare with peers"]
            )
        if node == "extract":
            return "{}"
        if node == "summary":
            return "Earlier, the user asked about Indian stocks and funds."
        return _answer(prompt)

    def ask(
        self, prompt: str, temperature: float = 0.0, max_tokens: int = 0, model: str | None = None
    ) -> str:
        faults.llm_call()  # TEST-06: realistic latency / flaky provider, when a faults file asks for it
        out = self._reply(prompt)
        self.last_usage = (max(1, len(prompt) // 4), max(1, len(out) // 4))
        return out

    chat = ask

    def stream(
        self, prompt: str, temperature: float = 0.0, max_tokens: int = 0, model: str | None = None
    ) -> Iterator[str]:
        text = self.ask(prompt, temperature, max_tokens, model)
        faults.llm_first_token()
        for i in range(0, len(text), 40):
            yield text[i : i + 40]
