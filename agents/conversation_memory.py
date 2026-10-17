"""
agents/conversation_memory.py
Shared conversation-history buffer for LLM providers.

Long chat threads used to just drop everything past the last 20 messages —
early context (a user's stated goal, a ticker discussed 30 turns ago) vanished
silently rather than degrading gracefully. This keeps the same 20-message
window sent to the model on every call (cost/latency unchanged), but folds
whatever falls out of that window into a running summary via one cheap LLM
call per overflow, and prepends that summary as a system message so older
context isn't lost outright.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import prompts

log = logging.getLogger(__name__)

# The UI's routing badge ("*🔀 Routed to: Equity*") is presentation, not
# conversation: it must never be fed back to the model as history.
_BADGE_RE = re.compile(r"^\s*\*🔀 Routed to:[^*\n]*\*\s*\n*")


def strip_badge(text: str) -> str:
    return _BADGE_RE.sub("", text or "", count=1)


# The collapsible "Sources" block under an answer (graph/provenance.py, UX-07) is
# provenance for the reader, not conversation: the model never needs it as history.
_SOURCES_RE = re.compile(r"\n*<details><summary>Sources \(\d+\)</summary>.*?</details>", re.DOTALL)


def strip_sources(text: str) -> str:
    return _SOURCES_RE.sub("", text or "")


# The one summarisation prompt (ARCH-03): every provider's overflow summariser
# and the orchestrator's history summary format it with old= and chunk=.
SUMMARY_PROMPT = prompts.get("memory.summary")


class ConversationMemory:
    def __init__(self, cap: int = 20, summarizer: Callable[[str, str], str] | None = None):
        self.cap = cap
        self._history: list[dict] = []
        self._summary = ""
        self._summarized_upto = 0
        self._summarizer = summarizer

    def add_user(self, content: str):
        self._history.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self._history.append({"role": "assistant", "content": content})

    def pop_last(self):
        """Drops the most recent turn — used to roll back a user message that never got a reply."""
        if self._history:
            self._history.pop()

    def load(self, messages: list[dict]):
        """Reseed from persisted chat history (session resume). Resets any prior summary
        since it was scoped to this in-process instance, not the reloaded transcript."""
        self._history = list(messages)
        self._summary = ""
        self._summarized_upto = 0

    def clear(self):
        self._history.clear()
        self._summary = ""
        self._summarized_upto = 0

    def _fold_overflow(self):
        if len(self._history) <= self.cap:
            return
        overflow_end = len(self._history) - self.cap
        if overflow_end <= self._summarized_upto:
            return
        new_chunk = self._history[self._summarized_upto : overflow_end]
        if self._summarizer is None:
            # No summarizer wired — still advance the cursor so we don't retry
            # every call; overflow is silently dropped (old behavior).
            self._summarized_upto = overflow_end
            return
        chunk_text = "\n".join(f"{m['role']}: {m['content']}" for m in new_chunk)
        try:
            self._summary = self._summarizer(self._summary, chunk_text)
            self._summarized_upto = overflow_end
        except Exception as e:
            log.warning(f"[ConversationMemory] summarization failed, keeping overflow unsummarized: {e}")
            # Don't advance the cursor — retry next call rather than losing the chunk silently.

    def context(self) -> list[dict]:
        """Messages to send to the model: an optional summary system-message
        (only when older turns have actually fallen out of the window) + the
        most recent `cap` raw messages."""
        self._fold_overflow()
        recent = self._history[-self.cap :]
        if self._summary:
            return [
                {"role": "system", "content": f"[Summary of earlier conversation]: {self._summary}"}
            ] + recent
        return recent

    def __len__(self):
        return len(self._history)
