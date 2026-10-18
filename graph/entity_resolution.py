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
    COMPANY_GROUPS,
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
# A bare group name ("Tata") is a company question even when the model called it chat.
_GROUP_INTENTS = _COMPANY_INTENTS | {"chat"}


# Hindi particles in Hinglish questions ("tcs ka share price kya hai").
_PARTICLES = frozenset(
    [
        "ka",
        "ki",
        "ke",
        "ko",
        "me",
        "mein",
        "se",
        "hai",
        "hain",
        "kya",
        "kaisa",
        "kaise",
        "kaisi",
        "batao",
        "bata",
        "dikhao",
        "wala",
        "wali",
        "vala",
    ]
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&.'-]*")


def _skip(word: str) -> bool:
    w = word.upper().strip(".")
    return w in STOP_WORDS or w in NON_COMPANY_TERMS or len(w) < 2 or word.lower() in _PARTICLES


def _phrases() -> list[str]:
    phrases = set(TICKER_ALIASES) | set(RENAMED) | {n.upper() for n in AMBIGUOUS_EXACT_NAMES}
    return sorted(phrases, key=len, reverse=True)


_NAME_METHODS = {"name", "alias", "renamed", "symbol"}


def extract_mentions(query: str) -> list[str]:
    text = _FUND.sub(lambda m: " " * len(m.group(0)), query or "")
    masked = list(text)
    found: list[tuple[int, str]] = []

    def take(start: int, end: int, mention: str) -> None:
        found.append((start, mention))
        for i in range(start, end):
            masked[i] = " "

    def free(start: int, end: int) -> bool:
        return all(
            c != " " or text[i] == " " for i, c in zip(range(start, end), masked[start:end], strict=False)
        )

    # 1. Full company names written out, longest first: "Kotak Mahindra Bank"
    #    must win over the single-word aliases "Kotak" and "Mahindra", and
    #    "TVS Motor" over the ambiguous "TVS". Strong fuzzy matches count too
    #    ("asain paints", "hdfc bnk"), but only for a 2+ word phrase.
    words = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
    for n in (4, 3, 2):
        for i in range(len(words) - n + 1):
            window = words[i : i + n]
            if _skip(window[0][2]) or _skip(window[-1][2]):
                continue
            start, end = window[0][0], window[-1][1]
            if not free(start, end):
                continue
            phrase = text[start:end]
            r = resolve(phrase)
            if r.status == "found" and (
                r.method in _NAME_METHODS or (r.method == "fuzzy" and len(phrase) >= 8)
            ):
                take(start, end, phrase)

    upper = text.upper()
    for phrase in _phrases():
        for m in re.finditer(rf"(?<![\w&]){re.escape(phrase)}(?![\w&])", upper):
            if "".join(masked[m.start() : m.end()]).strip() and free(m.start(), m.end()):
                take(m.start(), m.end(), text[m.start() : m.end()])

    for regex in (_SUFFIXED, _BSE_CODE):
        for m in regex.finditer("".join(masked)):
            take(m.start(), m.end(), m.group(0))

    # A bare business-group word ("Tata", "Adani") is a mention that must be asked about.
    for m in _WORD.finditer("".join(masked)):
        if m.group(0).lower() in COMPANY_GROUPS:
            take(m.start(), m.end(), m.group(0))

    # 2. Words right before "share(s)/stock": "zydus share price", "xyzq stock".
    #    Generic words ("value stocks", "energy stocks") must not become a
    #    company by fuzzy match: only an exact name/alias/symbol, an ambiguous
    #    group name, or a single ticker-like unknown word is kept.
    for m in _STOCK_WORD.finditer("".join(masked)):
        ws = m.group(1).split()
        while ws and _skip(ws[0]):
            ws.pop(0)
        while ws and _skip(ws[-1]):
            ws.pop()
        if not ws:
            continue
        phrase = " ".join(ws)
        r = resolve(phrase)
        keep = (
            (r.status == "found" and r.method in _NAME_METHODS | {"bse_code"})
            or (r.status == "ambiguous" and r.method == "name_prefix")
            or (r.status == "not_found" and len(ws) == 1 and 3 <= len(phrase) <= 12)
        )
        if keep:
            start = m.start(1) + m.group(1).find(ws[0])
            take(start, start + len(phrase), phrase)

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


def add_llm_mentions(result: EntityResult, llm_mentions: list, query: str = "") -> EntityResult:
    """Fold in the company mentions QueryAnalyzer's LLM found, resolving each
    against the master. Mentions already covered are skipped, including a word of
    a name the deterministic pass already matched: the model's "Relaince" next to
    the resolved "Relaince industries" must not ask "which Relaince?"."""
    covered = {normalise_name(u["query"]) for u in result.unresolved}
    spans = " | ".join(normalise_name(m) for m in extract_mentions(query)) if result.entities else ""
    extra: list[str] = []
    for raw in llm_mentions or []:
        if not isinstance(raw, str):
            continue
        mention = raw.strip()
        if not mention or all(_skip(w) for w in mention.split()):
            continue
        if normalise_name(mention) in covered or (spans and normalise_name(mention) in spans):
            continue
        r = resolve(mention)
        if r.status == "found" and r.symbol in result.entities:
            continue
        extra.append(mention)
    if extra:
        result.mentioned = True
        _resolve_all(extra, result)
    return result


def history_carry(history: list[dict]) -> tuple[list[str], list[str]]:
    """What a company-less follow-up refers to: (entity, ambiguous_candidates).

    The most recent user turn that named a company decides. One company is
    carried over; two or more (a comparison) are ambiguous, so the caller asks
    instead of silently picking one (LLM-10).
    """
    for msg in reversed(history or []):
        if msg.get("role") != "user":
            continue
        found = resolve_mentions(msg.get("content", "")).entities
        if len(found) == 1:
            return found, []
        if found:
            return [], found
    return [], []


def history_entities(history: list[dict]) -> list[str]:
    """The single company an earlier user turn settled on; [] if none or ambiguous."""
    return history_carry(history)[0]


def ambiguous_reference(candidates: list[str]) -> dict:
    """An unresolved entry for "its RSI" after a multi-company turn."""
    return {
        "query": "that",
        "status": "ambiguous",
        "reference": True,
        "candidates": [{"symbol": c, "name": c} for c in candidates],
    }


def blocking_resolutions(state: dict) -> list[dict]:
    """The unresolved mentions that must be answered before any research:
    every ambiguous one, and not-found ones when nothing else resolved and
    the turn is about a company."""
    unresolved = state.get("entity_resolutions") or []
    intents = {state.get("intent")} | set(state.get("secondary_intents") or [])
    if not unresolved:
        return []
    if not intents & _COMPANY_INTENTS:
        # Only a bare group word still asks, and only when the turn is general chat.
        if intents & _GROUP_INTENTS:
            return [
                u
                for u in unresolved
                if u["status"] == "ambiguous" and normalise_name(u["query"]) in COMPANY_GROUPS
            ]
        return []
    too_many = [u for u in unresolved if u["status"] == "too_many"]
    if too_many:
        return too_many
    ambiguous = [u for u in unresolved if u["status"] == "ambiguous"]
    if ambiguous:
        return ambiguous
    missing = [u for u in unresolved if u["status"] == "missing_company"]
    if missing:
        return missing
    if not state.get("entities"):
        return _without_fragments([u for u in unresolved if u["status"] == "not_found"])
    return []


def _without_fragments(not_found: list[dict]) -> list[dict]:
    """One phrase ("XYZ Widgets Ltd") can be found twice, as the whole name and as a
    fragment ("XYZ"). Keep the longest, so the reply says it once."""
    names = [normalise_name(u["query"]) for u in not_found]
    return [
        u
        for u, n in zip(not_found, names, strict=True)
        if not any(n != other and n and n in other for other in names)
    ]


def _option(c: dict) -> str:
    return f"{c['name']} (**{c['symbol']}**)"


def clarification_text(blocking: list[dict]) -> str:
    """Deterministic reply for unresolved names — no LLM, no tools."""
    parts: list[str] = []
    for u in blocking:
        name = u["query"]
        if u["status"] == "too_many":
            symbols = ", ".join(f"**{c['symbol']}**" for c in u["candidates"])
            parts.append(
                f"You asked about {len(u['candidates'])} companies ({symbols}). I can research up to "
                f"{u['limit']} at a time, so the answer stays fast and complete.\n\n"
                f"Tell me which {u['limit']} to start with — or, to compare many at once, ask the screener "
                'for a table, e.g. *"IT stocks with PE below 30"*.'
            )
            return "\n\n".join(parts)
        if u["status"] == "missing_company":
            parts.append(
                "Which company do you mean? Tell me its name or NSE symbol, for example "
                '*"How is TCS doing?"* or *"What is the debt of Infosys?"*.'
            )
            return "\n\n".join(parts)
        if u["status"] == "ambiguous" and u.get("reference"):
            symbols = " or ".join(f"**{c['symbol']}**" for c in u["candidates"])
            parts.append(f"You mentioned more than one company earlier. Do you mean {symbols}?")
        elif u["status"] == "ambiguous":
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


# ── Answers to "Which X do you mean?" ─────────────────────────────────────────

CLARIFY_MARKER = "Reply with the company name or its NSE symbol"
_OPTION = re.compile(r"([^\n(]*?)\s*\(\*\*([A-Z0-9&.\-]+)\*\*\)")
_REPLY_FILLER = frozenset(
    [
        "the",
        "one",
        "a",
        "an",
        "please",
        "i",
        "mean",
        "want",
        "it",
        "is",
        "its",
        "that",
        "this",
        "of",
        "for",
        "me",
        "go",
        "with",
        "pick",
        "choose",
        "take",
        "yes",
        "yeah",
        "yep",
        "ok",
        "okay",
        "sure",
    ]
)
_ORDINALS = {"first": 0, "1st": 0, "1": 0, "second": 1, "2nd": 1, "2": 1, "third": 2, "3rd": 2, "3": 2}


def pending_clarification(history: list[dict]) -> tuple[str, list[tuple[str, str]]] | None:
    """(the question that was asked, [(company name, symbol), ...]) when the
    assistant's last message was a "which company?" question and the newest
    message is the user's reply to it. None otherwise."""
    from agents.conversation_memory import strip_badge

    msgs = list(history or [])
    if msgs and msgs[-1].get("role") == "user":
        msgs.pop()  # the reply being analysed is already in the history
    if len(msgs) < 2 or msgs[-1].get("role") != "assistant" or msgs[-2].get("role") != "user":
        return None
    text = strip_badge(msgs[-1].get("content", ""))
    if CLARIFY_MARKER not in text:
        return None
    options = []
    for m in _OPTION.finditer(text):
        name = re.sub(r"^(?:[-*•]\s*|Did you mean\s+|,\s*)", "", m.group(1).strip()).strip()
        options.append((name, m.group(2)))
    if not options:
        return None
    return strip_badge(msgs[-2].get("content", "")), options


def match_clarification_reply(reply: str, options: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Which option a short reply picks: its symbol, its position ("the first
    one"), or words that fit exactly one option's name ("the bank one" fits
    HDFC Bank, not HDFC Life). None when it picks none or several."""
    words = re.findall(r"[a-z0-9&.\-]+", (reply or "").lower())
    if not words:
        return None
    for name, symbol in options:
        if symbol.lower() in words or " ".join(words) == name.lower():
            return name, symbol
    content = [w for w in words if w not in _REPLY_FILLER]
    for w in content:
        if w in _ORDINALS and _ORDINALS[w] < len(options):
            return options[_ORDINALS[w]]
    if not content:
        return None
    fits = [
        (n, s)
        for n, s in options
        if all(any(c in t for t in re.findall(r"[a-z0-9&]+", n.lower())) for c in content)
    ]
    return fits[0] if len(fits) == 1 else None


# A question that points at a company without naming one ("what about its debt?").
_COMPANY_POINTER = re.compile(
    r"\b(?:its|it's|it|this\s+(?:stock|company|share)|that\s+(?:stock|company|share)|the\s+(?:stock|company|share)|"
    r"they|their)\b",
    re.IGNORECASE,
)
_NOT_ABOUT_ONE_COMPANY = re.compile(
    r"\b(?:market|markets|nifty|sensex|sectors?|indices|index|stocks|shares|funds?|my\s+\w+|portfolio|"
    r"holdings|watchlist|reports?|alerts?|gainers|losers|movers)\b",
    re.IGNORECASE,
)


_FOLLOW_UP_LEAD = re.compile(
    r"^\s*(?:and|also|then|now|what\s+about|how\s+about|which\s+(?:one|has|is|of\s+(?:them|these|the\s+two)))\b",
    re.IGNORECASE,
)
_SHORT_FOLLOW_UP_WORDS = 4  # "RSI?", "and the PE" — a bare metric reads as a follow-up


def is_follow_up(query: str) -> bool:
    """True when a company-less message continues the previous company's thread
    ("its debt", "and the PE?", "RSI?"). A general question ("what does a high P/E
    mean?", "which stock should I buy?") names no company and points at none, so
    the earlier company must not be injected into it (live smoke 2026-10-18)."""
    from graph.data_questions import DATA_WORDS, DEFINITIONAL

    q = query or ""
    if _COMPANY_POINTER.search(q) or _FOLLOW_UP_LEAD.search(q):
        return True
    if DEFINITIONAL.search(q) or _NOT_ABOUT_ONE_COMPANY.search(q):
        return False
    return len(q.split()) <= _SHORT_FOLLOW_UP_WORDS and bool(DATA_WORDS.search(q))


def lacks_company(query: str, intent: str | None) -> bool:
    """True when a company question names none: it points at one ("its debt",
    "the stock") and says nothing broader, so the answer is to ask which."""
    q = query or ""
    return (
        intent in _COMPANY_INTENTS
        and bool(_COMPANY_POINTER.search(q))
        and not _NOT_ABOUT_ONE_COMPANY.search(q)
    )


def missing_company_entry() -> dict:
    return {"query": "", "status": "missing_company", "candidates": []}


# ── Fund names ("Compare Parag Parikh Flexi Cap and HDFC Flexi Cap") ───────────
# Funds are not company mentions, so the analyzer returns none for them; the fund
# tools read the names straight from the user's words instead of the "market" sentinel.

_FUND_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:compare|comparison\s+of|difference\s+between|show\s+me|tell\s+me\s+about|"
    r"how\s+(?:is|are|do|does)|which\s+is\s+better|is|"
    r"(?:what(?:'s|\s+is)\s+(?:the\s+)?)?(?:current\s+|latest\s+)?(?:nav|returns?|performance|cagr)\s+(?:of|for))[,:]?\s+(?:the\s+)?",
    re.IGNORECASE,
)
_FUND_SPLIT = re.compile(r"\s+(?:vs\.?|versus|and|with|or|against)\s+|\s*,\s*", re.IGNORECASE)
_FUND_TAIL_WORDS = frozenset(
    [
        "fund",
        "funds",
        "scheme",
        "schemes",
        "returns",
        "return",
        "performance",
        "nav",
        "cagr",
        "comparison",
        "compare",
        "better",
        "please",
        "mutual",
        "direct",
        "growth",
        "plan",
        "doing",
        "performing",
        "today",
        "now",
        "latest",
        "looking",
    ]
)


def extract_fund_names(query: str) -> list[str]:
    """The fund names a question compares or asks about, in the order written
    ("Compare A and B" gives [A, B]); [] when it names none."""
    text = _FUND_LEAD.sub("", (query or "").strip().rstrip("?.! "))
    names: list[str] = []
    for part in _FUND_SPLIT.split(text):
        words = part.split()
        while words and words[-1].lower().strip(".,") in _FUND_TAIL_WORDS:
            words.pop()
        name = " ".join(words).strip()
        if len(name) >= 4 and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names[:4]
