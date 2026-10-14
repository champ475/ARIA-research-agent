"""
agents/providers/local_llm.py
Last-resort fallback: a local OpenAI-compatible server the operator runs
(llama.cpp `llama-server`, Ollama, LM Studio, ...). LLMClient tries it only
after every cloud provider failed for this call, so a rate-limited or broken
Groq/Gemini (or BYO) key degrades to a slower, smaller model instead of an
error.

Unlike OpenAICompatibleProvider's BYO base_url, LOCAL_LLM_BASE_URL comes from
the operator's environment, not from a user, so it is trusted and may point
at localhost — the SEC-01 public-https check does not apply to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlparse

from agents.providers.openai_compatible import OpenAICompatibleProvider
from config import (
    LOCAL_LLM_API_KEY,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MAX_TOKENS,
    LOCAL_LLM_MODEL,
    LOCAL_LLM_TIMEOUT_SECONDS,
)


def local_llm_configured() -> bool:
    return bool(LOCAL_LLM_BASE_URL)


class LocalLLMProvider(OpenAICompatibleProvider):
    # A CPU model can take longer than the cloud stream cap just to process the
    # prompt, so both waits use the local budget instead (REL-02).
    _request_timeout = LOCAL_LLM_TIMEOUT_SECONDS
    _stream_timeout = LOCAL_LLM_TIMEOUT_SECONDS

    def __init__(self, system: str):
        if not LOCAL_LLM_BASE_URL:
            raise ValueError("LOCAL_LLM_BASE_URL is not set — local LLM fallback disabled.")
        if urlparse(LOCAL_LLM_BASE_URL).scheme not in ("http", "https"):
            raise ValueError("LOCAL_LLM_BASE_URL must be an http(s) URL.")
        try:
            from openai import OpenAI as _OpenAI
        except ImportError as e:
            raise ImportError("Run: pip install openai") from e
        # max_retries=0: the SDK's own retries would multiply a slow local
        # timeout; LLMClient._call already owns the retry policy.
        client = _OpenAI(
            api_key=LOCAL_LLM_API_KEY or "local",
            base_url=LOCAL_LLM_BASE_URL,
            timeout=LOCAL_LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        self._bind(client, LOCAL_LLM_MODEL, system)
        self.name = f"Local ({LOCAL_LLM_MODEL})"

    # Output is capped at LOCAL_LLM_MAX_TOKENS; the `model` override (Groq's
    # fast-vs-primary choice) is ignored, as for every non-Groq provider.

    def ask(
        self, prompt: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None
    ) -> str:
        return super().ask(prompt, temperature, min(max_tokens, LOCAL_LLM_MAX_TOKENS))

    def chat(
        self, msg: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None
    ) -> str:
        return super().chat(msg, temperature, min(max_tokens, LOCAL_LLM_MAX_TOKENS))

    def stream(
        self, msg: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None
    ) -> Iterator[str]:
        return super().stream(msg, temperature, min(max_tokens, LOCAL_LLM_MAX_TOKENS))
