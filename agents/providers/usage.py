"""Per-thread `last_usage` for LLM providers (REL-03).

Compiled graphs — and with them each node's LLMClient and providers — are
shared by every conversation that uses the same provider configuration, so
two turns can call the same provider object at once. The token usage a call
reports must be read back by the thread that made it, not by whichever call
finished last.
"""

from __future__ import annotations

import threading
from typing import Any


def stream_event_usage(event: Any) -> tuple[int, int] | None:
    """Token usage carried by one streamed event, if any: OpenAI-style
    `usage` on the final chunk, Groq's `x_groq.usage`, or google-genai's
    `usage_metadata` (LLM-11)."""
    from utils.tracing import usage_from_response

    found = usage_from_response(event)
    if found is None:
        x_groq = getattr(event, "x_groq", None)
        if x_groq is not None:
            found = usage_from_response(x_groq)
    return found


class ThreadLocalUsage:
    """Mixin: `self.last_usage` is stored per thread."""

    @property
    def last_usage(self) -> Any:
        slot = self.__dict__.get("_usage_slot")
        return getattr(slot, "value", None) if slot is not None else None

    @last_usage.setter
    def last_usage(self, value: Any) -> None:
        slot = self.__dict__.get("_usage_slot")
        if slot is None:
            slot = self.__dict__.setdefault("_usage_slot", threading.local())
        slot.value = value
