"""
utils/input_guard.py — the single entry contract for a chat message (VAL-01).

Everything downstream (entity regexes, intent classification, LLM prompts,
alert parsing) assumes a reasonably sized, printable string. This module is
the one place that guarantees it: Unicode NFKC normalisation (full-width
digits/letters become ASCII, ligatures split), removal of control and
invisible formatting characters (zero-width spaces would otherwise split
"TCS" into an unmatchable "T​CS"), whitespace collapsing, and an upper
length bound. It never raises; it returns a typed result.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from config import MAX_CHAT_CHARS

# Zero-width / invisible characters that are category Cf but worth naming
# explicitly; every other Cc/Cf/Co/Cs/Cn character is dropped by category.
_INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­"}
_KEEP = {"\n", "\t"}
_DROP_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}
_SPACES_RE = re.compile(r"[^\S\n]+")  # runs of whitespace other than newlines
_NEWLINES_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class CleanMessage:
    text: str


@dataclass(frozen=True)
class InputRejected:
    reason: Literal["empty", "too_long", "not_text"]
    user_message: str


def _strip_invisible(text: str) -> str:
    out = []
    for ch in text:
        if ch in _KEEP:
            out.append(ch)
        elif ch in _INVISIBLE or unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        else:
            out.append(ch)
    return "".join(out)


def clean_message(raw: object, max_chars: int = MAX_CHAT_CHARS) -> CleanMessage | InputRejected:
    """Normalise and validate one chat message."""
    if not isinstance(raw, str):
        return InputRejected("not_text", "That message couldn't be read as text — please type it again.")
    text = unicodedata.normalize("NFKC", raw)
    text = _strip_invisible(text)
    text = text.replace("\t", " ")
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _NEWLINES_RE.sub("\n\n", text).strip()
    if not text:
        return InputRejected("empty", "")
    if len(text) > max_chars:
        return InputRejected(
            "too_long",
            f"Your message is {len(text):,} characters; the limit is {max_chars:,}. "
            "Please shorten it or split it into separate questions.",
        )
    return CleanMessage(text)
