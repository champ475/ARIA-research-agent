"""
graph/confirmation.py — read a reply to a pending confirmation (VAL-08).

When the graph pauses ("This plan has 12 steps — proceed?"), the next message
is classified deterministically:

    yes           "yes", "sure", "go ahead", "yes please", "haan", "ok 👍"
    no            "no", "nah", "cancel", "not now", "nahi"
    new_question  anything that reads as a fresh request ("what about INFY?")
    unclear       a short reply that is neither ("hmm", "maybe")

A new question cancels the pending step and runs as its own turn — it is
never silently counted as a "no". An unclear reply is asked about again.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Reply = Literal["yes", "no", "new_question", "unclear"]

_YES = (
    r"y|ya|yes|yeah|yea|yep|yup|yess+|sure|ok|okay|k|kk|okie|fine|alright|all right|correct|right|"
    r"go|go ahead|go on|proceed|continue|approve|approved|confirm|confirmed|do it|please do|"
    r"sounds good|absolutely|of course|definitely|why not|show anyway|show it anyway|show me anyway|"
    r"haan|haa|ha|han|ji|ji haan|haan ji|theek hai|thik hai|chalo|kar do|karo"
)
_NO = (
    r"n|no|nope|nah|naa|na|cancel|stop|abort|skip|don'?t|do not|never ?mind|not now|no thanks|"
    r"no thank you|leave it|forget it|not needed|nahi|nahin|mat karo|rehne do|ruko"
)
_POLITE = r"(?:\s*(?:please|pls|plz|thanks|thank you|thx|ji))*"
_YES_RE = re.compile(rf"^(?:{_YES})(?:\s*,?\s*(?:{_YES}))*{_POLITE}$")
_NO_RE = re.compile(rf"^(?:{_NO})(?:\s*,?\s*(?:{_NO}))*{_POLITE}$")
_QUESTION_WORDS = re.compile(
    r"\b(?:what|how|why|which|when|show|tell|compare|analy[sz]e|price|pe|rsi|news|report|alert|"
    r"fund|stock|share|about|instead|also|kya|kaisa|batao)\b"
)


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    # Emoji and punctuation carry no meaning here ("ok 👍", "yes!!").
    t = "".join(ch if ch.isalnum() or ch in " '&" else " " for ch in t)
    return " ".join(t.split())


def classify_reply(text: str) -> Reply:
    raw = (text or "").strip()
    t = _normalise(raw)
    if not t:
        return "unclear"
    if _YES_RE.match(t):
        return "yes"
    if _NO_RE.match(t):
        return "no"
    if "?" in raw or len(t.split()) >= 4 or _QUESTION_WORDS.search(t):
        return "new_question"
    return "unclear"


REPROMPT = "Please answer **yes** to go ahead or **no** to cancel — or just ask a new question."
