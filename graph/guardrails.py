"""
graph/guardrails.py — financial-safety guardrails (LLM-09) for the
education-only posture (COMP-01, docs/ADVICE_POLICY.md).

- `check_input(message)` runs before the graph: requests to act on insider
  information, to manipulate a price, or for guaranteed returns are refused
  with a fixed reply (no research, no LLM call); requests for a personal
  buy/sell call or a tip are *reframed* — answered, but as analysis without a
  call; unlisted/penny/grey-market requests also get a caution. Asking *about*
  these topics ("what is a pump and dump?") is education and allowed.
- `check_output(text)` removes sentences that promise returns or make a
  personal buy/sell call, unless the sentence is itself a negation ("no
  investment can guarantee returns"), and says that it did.
- `finalize(text, state)` applies the output check and the deterministic
  footer for the answer type. Footers are added here, in code — never left
  to the LLM.

Rules only: deterministic, instant, and testable against the red-team set in
tests/test_guardrails.py. (The PRD's optional classifier prompt is not used —
an extra LLM call per turn costs too much on the local fallback model.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from graph.advice_policy import CAUTION_NOTE, FOOTER_MARKER, FOOTERS, REFUSALS, REWRITE_NOTE
from tools.analysis.fundamental import number_footnotes

_I = re.IGNORECASE

# Wanting to act, as opposed to asking what something is.
_WANTS = r"(?:how\s+(?:do|can|should)\s+(?:i|we)|help\s+(?:me|us)|let'?s|i\s+want\s+to|we\s+want\s+to|plan\s+to|teach\s+me\s+to|join|organi[sz]e|coordinate|i'?ll|we'?ll)"

_INSIDER = re.compile(
    r"\b(?:i|we|my\s+\w+|someone|a\s+friend)(?:\s+\w+){0,3}?\s+(?:have|has|got|know|knows|heard)\b[^.?!]{0,60}"
    r"\b(?:inside(?:r)?\s+(?:info\w*|news|tip\w*|knowledge)|unpublished|upsi|leak(?:ed)?)"
    r"|\b(?:give|tell|share|send)\s+(?:me|us)\s+(?:some\s+)?inside(?:r)?\s+(?:info\w*|news|tips?)"
    r"|\bbefore\s+(?:the\s+)?(?:results?|announcement|news|merger|deal)\s+(?:is|are|comes?|goes|gets?)\s+(?:out|public|announced)"
    r"|\b(?:trade|buy|sell)\b[^.?!]{0,40}\b(?:inside(?:r)?\s+(?:info\w*|tip\w*)|upsi|leaked\s+(?:results|numbers))",
    _I,
)
_MANIPULATION = re.compile(
    rf"\b{_WANTS}\b[^.?!]{{0,40}}\b(?:pump|manipulat\w*|ramp\s+up|jack\s+up|rig|corner|spoof|wash\s+trad\w*|circular\s+trad\w*)"
    r"|\b(?:pump\s*(?:and|&|n)\s*dump|operator)\s+(?:group|call|stock|tip)s?\b[^.?!]{0,30}\b(?:join|buy|which|give|send|tell)"
    r"|\b(?:join|buy\s+into)\s+(?:a\s+|an\s+|the\s+)?(?:pump|operator)\b"
    r"|\b(?:move|push|drive)\s+(?:the|its|a)\s+(?:stock\s+)?price\s+(?:up|down)\s+(?:together|with\s+(?:my|our)\s+(?:group|friends))",
    _I,
)
_GUARANTEE = re.compile(
    r"\bguarantee(?:d|s)?\s+(?:(?:me|us|my|our|that)\s+)*(?:\d+\s*%\s*)?"
    r"(?:(?:monthly|weekly|daily|yearly|annual|fixed|assured|steady)\s+)?(?:returns?|profits?|gains?|income|money)"
    r"|\bguarantee\s+(?:that\s+)?(?:i|we|you)\b[^.?!]{0,30}\b(?:make|earn|get|receive|profit)\b"
    r"|\b(?:\d+\s*%\s*)guaranteed\b"
    r"|\bassured\s+(?:\d+\s*%\s*)?(?:returns?|profits?|gains?)"
    r"|\bsure[\s-]*shot\b|\bdouble\s+(?:my|our)\s+money\b|\bcan'?t\s+lose\b|\bno[\s-]+risk\s+(?:returns?|profits?)"
    r"|\b100\s*%\s*(?:safe|sure|guaranteed|returns?)"
    r"|\brisk[\s-]*free\s+(?:returns?|profits?|stocks?|investment|trades?)",
    _I,
)
_GUARANTEE_OK = re.compile(
    r"\b(?:fd|fixed\s+deposits?|ppf|nsc|government\s+(?:bond|securit)\w*|g-?secs?|t-?bills?|rbi\s+bonds?)\b",
    _I,
)
_PERSONAL_CALL = re.compile(
    r"\bshould\s+i\s+(?:buy|sell|exit|hold|book\s+profits?|average|enter|switch|keep)\b"
    r"|\b(?:buy|sell)\s+(?:it\s+)?(?:now|today|tomorrow|this\s+week|or\s+(?:sell|buy|hold|wait))\b"
    r"|\btarget\s+price\b|\bstock\s+tips?\b|\btips?\s+for\s+(?:today|tomorrow|intraday|this\s+week)"
    r"|\bmulti[\s-]*baggers?\b|\bintraday\s+(?:calls?|tips?)\b"
    r"|\bwhich\s+(?:stocks?|shares?|funds?)\s+(?:should|to|can|do)\s+i\s+(?:buy|invest|pick)"
    r"|\bbest\s+(?:stocks?|shares?)\s+to\s+buy\b|\bgive\s+me\s+(?:a\s+)?(?:stock|tip|call|pick)s?\b"
    r"|\bwill\s+[\w&.-]+(?:\s+[\w&.-]+)?\s+(?:go\s+up|go\s+down|rise|fall|crash|double)\b"
    r"|\bwhich\s+(?:stocks?|shares?|funds?)\s+(?:will|would|is\s+going\s+to|are\s+going\s+to)\s+"
    r"(?:double|triple|multiply|go\s+up|rise|rally|zoom|jump)\b"
    r"|\bis\s+[\w&.' -]{1,30}?\s+(?:a\s+)?(?:good\s+|great\s+|safe\s+)?(?:buy|sell)\b"
    r"|\bis\s+it\s+(?:a\s+)?(?:good|right|safe)\s+(?:time|moment|day)\s+to\s+(?:buy|sell|invest|enter|exit)\b",
    _I,
)
_CAUTION = re.compile(
    r"\b(?:penny\s+stocks?|unlisted|pre[\s-]*ipo|grey\s+market|gmp|sme\s+(?:ipo|stocks?))\b", _I
)


@dataclass(frozen=True)
class InputVerdict:
    action: Literal["allow", "refuse", "reframe"]
    category: str | None = None
    reply: str | None = None
    caution: bool = False


def check_input(message: str) -> InputVerdict:
    text = message or ""
    if _INSIDER.search(text):
        return InputVerdict("refuse", "insider", REFUSALS["insider"])
    if _MANIPULATION.search(text):
        return InputVerdict("refuse", "manipulation", REFUSALS["manipulation"])
    if _GUARANTEE.search(text) and not _GUARANTEE_OK.search(text):
        return InputVerdict("refuse", "guarantee", REFUSALS["guarantee"])
    caution = bool(_CAUTION.search(text))
    if _PERSONAL_CALL.search(text) or caution:
        return InputVerdict(
            "reframe", "personal_call" if _PERSONAL_CALL.search(text) else "speculative", caution=caution
        )
    return InputVerdict("allow")


# ── output ────────────────────────────────────────────────────────────────────

_OUT_PROMISE = re.compile(
    r"\b(?:guarantee[sd]?|assured\s+returns?|risk[\s-]*free(?!\s+rate)|sure[\s-]*shot|can(?:'|no)t\s+lose"
    r"|will\s+definitely|is\s+(?:certain|bound)\s+to\s+(?:rise|go\s+up|double|grow)|100\s*%\s*(?:safe|sure|guaranteed))\b",
    _I,
)
_OUT_CALL = re.compile(
    r"\b(?:you\s+should|i\s+(?:would\s+)?(?:recommend|suggest)(?:ing)?|my\s+recommendation\s+is\s+to|we\s+recommend)\s+"
    r"(?:that\s+you\s+)?(?:buy|sell|exit|accumulate|short)(?:ing)?\b"
    r"|\b(?:strong\s+(?:buy|sell)|buy\s+now|sell\s+now|must[\s-]+buy|top\s+pick\s+for\s+you)\b"
    r"|\b(?:our|my)\s+target(?:\s+price)?\s+(?:is|of)\b",
    _I,
)
_NEGATION = re.compile(
    r"\b(?:no|not|never|cannot|can'?t|isn'?t|aren'?t|nothing|neither|without|beware|warning)\b", _I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\"'(₹-])")


def check_output(text: str) -> tuple[str, list[str]]:
    """(cleaned text, removed sentences). Works line by line so tables and
    headings survive; within a line, sentence by sentence."""
    removed: list[str] = []
    out_lines = []
    for line in (text or "").split("\n"):
        kept = []
        for sentence in _SENTENCE.split(line):
            offending = _OUT_PROMISE.search(sentence) or _OUT_CALL.search(sentence)
            # A negation elsewhere in the sentence ("no fund can guarantee
            # returns") makes it a warning, not a promise — but the matched
            # phrase's own words ("can't lose") don't count.
            rest = sentence[: offending.start()] + sentence[offending.end() :] if offending else sentence
            if offending and not _NEGATION.search(rest):
                removed.append(sentence.strip())
                continue
            kept.append(sentence)
        out_lines.append(" ".join(kept) if kept else ("" if line.strip() else line))
    return "\n".join(out_lines), removed


_VIEW = re.compile(
    r"\b(?:undervalued|overvalued|outlook|upside|downside|bullish|bearish|attractive|expensive|cheap|"
    r"accumulate|overweight|underweight|buy|sell|hold)\b",
    _I,
)


def _built_strategy(state: dict) -> bool:
    journal = state.get("research_journal") or []
    return any(
        e.get("node") == "CommunicationLayer" and "built strategy" in str(e.get("decision")) for e in journal
    )


def answer_kind(state: dict, text: str) -> str | None:
    intents = {state.get("intent")} | set(state.get("secondary_intents") or [])
    if "planning" in intents:
        return "planning" if _built_strategy(state) else None
    if "report" in intents:
        return "report"
    if intents & {"equity", "mf", "screener"}:
        return "research" if state.get("tasks") else None
    # A market or general answer that was built from fetched data is research too
    # (it carried no footer when the model had called the turn "chat").
    if (state.get("tasks") or state.get("evidence")) and "alert" not in intents:
        return "research"
    if _VIEW.search(text or ""):
        return "research"
    return None


def finalize(text: str, state: dict) -> str:
    """The answer as the user sees it: unsafe sentences removed (and said
    so), plus the footer for its kind. Idempotent."""
    from graph.entity_resolution import blocking_resolutions

    if not text or blocking_resolutions(state):
        return text
    cleaned, removed = check_output(text)
    if removed:
        cleaned = cleaned.rstrip() + "\n\n" + REWRITE_NOTE
    if state.get("advice_caution") and CAUTION_NOTE not in cleaned:
        cleaned = cleaned.rstrip() + "\n\n" + CAUTION_NOTE
    kind = answer_kind(state, cleaned)
    if kind and FOOTER_MARKER not in cleaned:
        cleaned = cleaned.rstrip() + "\n\n---\n" + FOOTERS[kind]
    return number_footnotes(cleaned)
