"""
graph/entity_resolution.py — which companies a message is about (VAL-02).

1. `extract_mentions` finds company mentions deterministically: curated
   aliases and known names, renamed symbols, `X.NS`/`X.BO`, 6-digit BSE codes,
   tickers typed in capitals, curated symbols in any case, and words right
   before "share(s)"/"stock". Mutual-fund names ("HDFC Flexi Cap Fund") are
   masked first so a fund question never turns into a bank stock.
2. QueryAnalyzer's LLM call adds the company mentions *as the user wrote
   them* (typos included). They are only ever resolved against the
   instrument master — the LLM can't invent a ticker.
3. Each mention resolves to found / ambiguous / not_found
   (tools/data/instruments.py).

Conversation history supplies the entity ONLY when the message mentions no
company at all ("what about its RSI?"). An unknown name no longer silently
becomes the previous turn's ticker — the XYZQ-after-TCS bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tools.data.instruments import (
    AMBIGUOUS_EXACT_NAMES,
    NON_COMPANY_TERMS,
    RENAMED,
    Resolution,
    normalise_name,
    resolve,
)
from utils.helpers import STOP_WORDS, TICKER_ALIASES

# "<up to 5 words> fund/scheme/etf/fof" — masked before extraction.
_FUND = re.compile(r"\b(?:[A-Za-z&.'-]+\s+){1,5}(?:fund|funds|scheme|etf|fof)\b", re.IGNORECASE)
_STOCK_WORD = re.compile(
    r"\b((?:[A-Za-z][\w&.'-]*\s+){1,3})(?:share|shares|stock|stocks|scrip)\b", re.IGNORECASE
)
_UPPER_TOKEN = re.compile(r"\b([A-Z][A-Z0-9&-]{1,14})\b")
_ANY_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9&-]{1,14})\b")
_SUFFIXED = re.compile(r"\b([A-Za-z][A-Za-z0-9&-]{1,14})\.(NS|BO)\b", re.IGNORECASE)
_BSE_CODE = re.compile(r"\b(5\d{5})\b")

# Intents where an unknown company stops the turn with "couldn't find X";
# elsewhere (mf, screener, planning, chat) a stray capitalised word must not.
_COMPANY_INTENTS = {"equity", "report", "alert"}


def _skip(word: str) -> bool:
    w = word.upper().strip(".")
    return w in STOP_WORDS or w in NON_COMPANY_TERMS or len(w) < 2


def _phrases() -> list[str]:
    phrases = set(TICKER_ALIASES) | set(RENAMED) | {n.upper() for n in AMBIGUOUS_EXACT_NAMES}
    return sorted(phrases, key=len, reverse=True)


def extract_mentions(query: str) -> list[str]:
    text = _FUND.sub(lambda m: " " * len(m.group(0)), query or "")
    masked = list(text)
    found: list[tuple[int, str]] = []

    def take(start: int, end: int, mention: str) -> None:
        found.append((start, mention))
        for i in range(start, end):
            masked[i] = " "

    upper = text.upper()
    for phrase in _phrases():
        for m in re.finditer(rf"(?<![\w&]){re.escape(phrase)}(?![\w&])", upper):
            if "".join(masked[m.start() : m.end()]).strip():
                take(m.start(), m.end(), text[m.start() : m.end()])

    for regex in (_SUFFIXED, _BSE_CODE):
        for m in regex.finditer("".join(masked)):
            take(m.start(), m.end(), m.group(0))

    # Words right before "share(s)/stock": "zydus share price", "xyzq stock".
    for m in _STOCK_WORD.finditer("".join(masked)):
        words = m.group(1).split()
        while words and _skip(words[0]):
            words.pop(0)
        if words and not all(_skip(w) for w in words):
            start = m.start(1) + m.group(1).find(words[0])
            take(start, start + len(" ".join(words)), " ".join(words))

    from config import STOCK_UNIVERSE

    remaining = "".join(masked)
    letters = [c for c in remaining if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6
    for m in _ANY_TOKEN.finditer(remaining):
        token = m.group(1)
        if _skip(token):
            continue
        typed_as_ticker = token.isupper() and not shouting
        if typed_as_ticker or token.upper() in STOCK_UNIVERSE:
            take(m.start(), m.end(), token)

    seen: set[str] = set()
    out: list[str] = []
    for _, mention in sorted(found):
        key = normalise_name(mention)
        if key and key not in seen:
            seen.add(key)
            out.append(mention.strip())
    return out


@dataclass
class EntityResult:
    entities: list[str] = field(default_factory=list)  # resolved NSE symbols, in mention order
    unresolved: list[dict] = field(default_factory=list)  # Resolution.as_dict() for ambiguous/not_found
    notes: list[str] = field(default_factory=list)  # e.g. "Zomato Ltd is now listed as Eternal"
    mentioned: bool = False  # the message named at least one company


def _resolve_all(mentions: list[str], result: EntityResult) -> None:
    for mention in mentions:
        r: Resolution = resolve(mention)
        if r.status == "found" and r.symbol:
            if r.symbol not in result.entities:
                result.entities.append(r.symbol)
            if r.note and r.note not in result.notes:
                result.notes.append(r.note)
        elif not any(u["query"].lower() == mention.lower() for u in result.unresolved):
            result.unresolved.append(r.as_dict())


def resolve_mentions(query: str) -> EntityResult:
    """Deterministic pass (before the LLM call)."""
    result = EntityResult()
    mentions = extract_mentions(query)
    result.mentioned = bool(mentions)
    _resolve_all(mentions, result)
    return result


def add_llm_mentions(result: EntityResult, llm_mentions: list) -> EntityResult:
    """Fold in the company mentions QueryAnalyzer's LLM found, resolving each
    against the master. Mentions already covered are skipped."""
    covered = {normalise_name(u["query"]) for u in result.unresolved}
    extra: list[str] = []
    for raw in llm_mentions or []:
        if not isinstance(raw, str):
            continue
        mention = raw.strip()
        if not mention or all(_skip(w) for w in mention.split()):
            continue
        if normalise_name(mention) in covered:
            continue
        r = resolve(mention)
        if r.status == "found" and r.symbol in result.entities:
            continue
        extra.append(mention)
    if extra:
        result.mentioned = True
        _resolve_all(extra, result)
    return result


def history_entities(history: list[dict]) -> list[str]:
    """The most recently mentioned resolvable company in earlier user turns."""
    for msg in reversed(history or []):
        if msg.get("role") != "user":
            continue
        found = resolve_mentions(msg.get("content", "")).entities
        if found:
            return found[:1]
    return []


def blocking_resolutions(state: dict) -> list[dict]:
    """The unresolved mentions that must be answered before any research:
    every ambiguous one, and not-found ones when nothing else resolved and
    the turn is about a company."""
    unresolved = state.get("entity_resolutions") or []
    intents = {state.get("intent")} | set(state.get("secondary_intents") or [])
    if not unresolved or not intents & _COMPANY_INTENTS:
        return []
    ambiguous = [u for u in unresolved if u["status"] == "ambiguous"]
    if ambiguous:
        return ambiguous
    if not state.get("entities"):
        return [u for u in unresolved if u["status"] == "not_found"]
    return []


def _option(c: dict) -> str:
    return f"{c['name']} (**{c['symbol']}**)"


def clarification_text(blocking: list[dict]) -> str:
    """Deterministic reply for unresolved names — no LLM, no tools."""
    parts: list[str] = []
    for u in blocking:
        name = u["query"]
        if u["status"] == "ambiguous":
            options = "\n".join(f"- {_option(c)}" for c in u["candidates"])
            parts.append(f"Which **{name}** do you mean?\n{options}")
        else:
            line = f"I couldn't find a company called **{name}** listed on NSE."
            if u.get("note"):
                line += f" {u['note']}"
            if u["candidates"]:
                line += " Did you mean " + ", ".join(_option(c) for c in u["candidates"]) + "?"
            parts.append(line)
    parts.append("Reply with the company name or its NSE symbol and I'll pick it up from there.")
    return "\n\n".join(parts)


def entity_notes_text(state: dict) -> str:
    """Notes for an answer that did run: renamed symbols, and names that
    couldn't be found alongside ones that could."""
    lines = list(state.get("entity_notes") or [])
    if state.get("entities"):
        for u in state.get("entity_resolutions") or []:
            if u["status"] == "not_found":
                lines.append(
                    f"Couldn't find a listed company called “{u['query']}”, so it isn't covered here."
                )
    return "".join(f"\n\nℹ️ {line}" for line in lines)
