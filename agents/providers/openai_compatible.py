"""
agents/providers/openai_compatible.py
One adapter for every provider that speaks the OpenAI chat-completions
shape — OpenAI itself, Groq, OpenRouter, Together, Fireworks, Ollama, and
anything else exposing an OpenAI-compatible `base_url`. This is the only
BYO-key path shipped today: Anthropic/Gemini users route through OpenRouter
with the same key instead of a native adapter (see ARCHITECTURE.md).

Mirrors agents/llm_client.py's _GroqProvider/_GeminiProvider shape exactly
(ask/chat/stream/reset/load_history, ConversationMemory-backed) so
LLMClient can swap this in as `self._primary` without any caller-visible
difference.
"""

from __future__ import annotations

from agents.conversation_memory import SUMMARY_PROMPT, ConversationMemory
from agents.providers.usage import ThreadLocalUsage
from config import LLM_STREAM_TIMEOUT_SECONDS, LLM_TIMEOUT_SECONDS, TOKENS_FAST
from utils.errors import ConfigurationError
from utils.tracing import usage_from_response


class OpenAICompatibleProvider(ThreadLocalUsage):
    # REL-02 per-request timeouts; LocalLLMProvider overrides both (CPU model).
    _request_timeout: float = LLM_TIMEOUT_SECONDS
    _stream_timeout: float = LLM_STREAM_TIMEOUT_SECONDS

    def __init__(self, system: str, api_key: str, model: str, base_url: str | None = None):
        if not api_key:
            raise ConfigurationError(
                "No API key is configured for your LLM provider.",
                fix_hint="Add one in Settings — any OpenAI-compatible provider works "
                "(OpenAI, Groq, OpenRouter, Together, Fireworks, Ollama, ...).",
            )
        if not model:
            raise ConfigurationError(
                "No model is configured for your LLM provider.", fix_hint="Set one in Settings."
            )
        try:
            from openai import OpenAI as _OpenAI
        except ImportError as e:
            raise ImportError("Run: pip install openai") from e
        # SEC-01: a user-supplied base_url must be a public https endpoint —
        # checked here (covers configs saved before validation existed) and
        # again on every request by SafeEgressTransport (DNS rebinding).
        from utils.url_safety import safe_http_client, validate_public_https_url

        if base_url:
            base_url = validate_public_https_url(base_url)
        client = _OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            http_client=safe_http_client(timeout=LLM_TIMEOUT_SECONDS),
            max_retries=0,  # LLMClient owns retries (REL-02)
        )
        self._bind(client, model, system)
        # Instance attribute (not the usual class-level `name`) since the
        # displayed provider name should reflect what the user configured.
        self.name = f"{model} ({base_url})" if base_url else model

    def _bind(self, client, model: str, system: str) -> None:
        """Shared tail of __init__, so a subclass that builds its client
        differently (agents/providers/local_llm.py) reuses ask/chat/stream."""
        self._client = client
        self._model = model
        self._system = system
        self._memory = ConversationMemory(cap=20, summarizer=self._summarize)

    def _summarize(self, old_summary: str, chunk_text: str) -> str:
        prompt = SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        r = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=TOKENS_FAST,
        )
        self.last_usage = usage_from_response(r)
        return r.choices[0].message.content.strip()

    def ask(
        self, prompt: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None
    ) -> str:
        # `model` override is a Groq-only concept (fast vs primary model,
        # see graph/nodes/{planner,query_analyzer,reflection,verification}.py)
        # — a BYO user's configured model is authoritative, same stance
        # agents/llm_client.py's _GeminiProvider already takes.
        msgs = [{"role": "system", "content": self._system}, {"role": "user", "content": prompt}]
        r = self._client.chat.completions.create(
            model=self._model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=self._request_timeout,
        )
        self.last_usage = usage_from_response(r)
        return r.choices[0].message.content.strip()

    def chat(
        self, msg: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None
    ) -> str:
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        r = self._client.chat.completions.create(
            model=self._model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=self._request_timeout,
        )
        self.last_usage = usage_from_response(r)
        reply = r.choices[0].message.content.strip()
        self._memory.add_assistant(reply)
        return reply

    def stream(self, msg: str, temperature: float = 0.3, max_tokens: int = 2000, model: str | None = None):
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        chunks: list[str] = []
        try:
            events = self._client.chat.completions.create(
                model=self._model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                timeout=self._stream_timeout,
            )
            for event in events:
                delta = event.choices[0].delta.content
                if delta:
                    chunks.append(delta)
                    yield delta
        except Exception:
            if not chunks:
                self._memory.pop_last()
            raise
        self._memory.add_assistant("".join(chunks))

    def reset(self):
        self._memory.clear()

    def load_history(self, messages: list[dict]):
        self._memory.load(messages)
