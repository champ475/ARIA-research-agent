"""
agents/llm_client.py
Groq round-robin key rotation → Gemini fallback.
Each LLMClient instance gets its own conversation history.
"""

import time
import logging
import threading
from config import (
    GROQ_API_KEYS, GROQ_MODEL, GROQ_MODEL_FAST, GEMINI_API_KEY, GEMINI_MODEL,
    PREFERRED_PROVIDER, MAX_RETRIES,
    TOKENS_SYNTHESIS, TOKENS_FAST, LLM_TEMPERATURE,
)
from agents.conversation_memory import ConversationMemory

_SUMMARY_PROMPT = (
    "Existing summary of earlier conversation (may be empty):\n{old}\n\n"
    "New older messages to fold in:\n{chunk}\n\n"
    "Write an updated concise summary (3-5 sentences) capturing key facts, figures, "
    "tickers, and decisions mentioned, so later replies stay consistent with this history. "
    "Return only the summary text."
)

log = logging.getLogger(__name__)

# ── Thread-safe Groq key rotator ─────────────────────────────────────────────

class _KeyRotator:
    def __init__(self, keys: list[str]):
        self._keys = keys
        self._idx  = 0
        self._lock = threading.Lock()

    def next(self) -> str | None:
        with self._lock:
            if not self._keys:
                return None
            key = self._keys[self._idx % len(self._keys)]
            self._idx += 1
            return key

    def available(self) -> bool:
        return bool(self._keys)


_groq_rotator = _KeyRotator(GROQ_API_KEYS)


# ── Provider wrappers ─────────────────────────────────────────────────────────

class _GroqProvider:
    name = "Groq"

    def __init__(self, system: str):
        if not _groq_rotator.available():
            raise ValueError(
                "No GROQ_API_KEY found.\n"
                "Add GROQ_API_KEY_1 (and optionally _2, _3) to your .env.\n"
                "Free keys at https://console.groq.com"
            )
        try:
            from groq import Groq as _Groq
            self._Groq = _Groq
        except ImportError:
            raise ImportError("Run: pip install groq")
        self._system  = system
        self._memory  = ConversationMemory(cap=20, summarizer=self._summarize)

    def _client(self):
        return self._Groq(api_key=_groq_rotator.next())

    def _summarize(self, old_summary: str, chunk_text: str) -> str:
        prompt = _SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        r = self._client().chat.completions.create(
            model=GROQ_MODEL_FAST, messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=TOKENS_FAST,
        )
        return r.choices[0].message.content.strip()

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        msgs = [{"role": "system", "content": self._system},
                {"role": "user",   "content": prompt}]
        r = self._client().chat.completions.create(
            model=model or GROQ_MODEL, messages=msgs,
            temperature=temperature, max_tokens=max_tokens,
        )
        return r.choices[0].message.content.strip()

    def chat(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        r = self._client().chat.completions.create(
            model=model or GROQ_MODEL, messages=msgs,
            temperature=temperature, max_tokens=max_tokens,
        )
        reply = r.choices[0].message.content.strip()
        self._memory.add_assistant(reply)
        return reply

    def stream(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None):
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        chunks: list[str] = []
        try:
            events = self._client().chat.completions.create(
                model=model or GROQ_MODEL, messages=msgs,
                temperature=temperature, max_tokens=max_tokens, stream=True,
            )
            for event in events:
                delta = event.choices[0].delta.content
                if delta:
                    chunks.append(delta)
                    yield delta
        except Exception:
            if not chunks:
                self._memory.pop_last()   # never produced output — don't leave an orphan turn
            raise
        self._memory.add_assistant("".join(chunks))

    def reset(self):
        self._memory.clear()

    def load_history(self, messages: list[dict]):
        self._memory.load(messages)


class _GeminiProvider:
    name = "Gemini"

    def __init__(self, system: str):
        if not GEMINI_API_KEY:
            raise ValueError(
                "No GEMINI_API_KEY found.\n"
                "Add it to your .env — free at https://aistudio.google.com/apikey"
            )
        try:
            from google import genai
            from google.genai import types as gt
            self._client = genai.Client(api_key=GEMINI_API_KEY)
            self._types  = gt
        except ImportError:
            raise ImportError("Run: pip install google-genai")
        self._system  = system
        self._memory  = ConversationMemory(cap=20, summarizer=self._summarize)

    def _summarize(self, old_summary: str, chunk_text: str) -> str:
        prompt = _SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        cfg = self._types.GenerateContentConfig(temperature=0.2, max_output_tokens=TOKENS_FAST)
        r = self._client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        return r.text.strip()

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        # model override is a Groq-only concept (fast vs primary model) — Gemini
        # is already the fallback tier, so it always uses GEMINI_MODEL.
        cfg = self._types.GenerateContentConfig(
            system_instruction=self._system,
            temperature=temperature, max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=cfg,
        )
        return r.text.strip()

    def _system_and_contents(self):
        """Gemini's Content only accepts user/model roles — a summary system
        message from ConversationMemory gets folded into system_instruction
        instead of injected as a fake turn."""
        ctx = self._memory.context()
        system = self._system
        turns = ctx
        if ctx and ctx[0]["role"] == "system":
            system = f"{self._system}\n\n{ctx[0]['content']}"
            turns = ctx[1:]
        contents = [
            self._types.Content(
                role="user" if t["role"] == "user" else "model",
                parts=[self._types.Part(text=t["content"])],
            )
            for t in turns
        ]
        return system, contents

    def chat(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        self._memory.add_user(msg)
        system, contents = self._system_and_contents()
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature, max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=cfg,
        )
        reply = r.text.strip()
        self._memory.add_assistant(reply)
        return reply

    def stream(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None):
        self._memory.add_user(msg)
        system, contents = self._system_and_contents()
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature, max_output_tokens=max_tokens,
        )
        chunks: list[str] = []
        try:
            for event in self._client.models.generate_content_stream(
                model=GEMINI_MODEL, contents=contents, config=cfg,
            ):
                delta = event.text
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


# ── Unified client ────────────────────────────────────────────────────────────

_DEFAULT_SYSTEM = (
    "You are ARIA, an expert institutional equity research analyst for Indian markets (NSE/BSE). "
    "Always respond with precise, data-grounded analysis. Format in clean Markdown."
)


class LLMClient:
    """
    With no `provider_config`: tries Groq (with key rotation) first, falls
    back to Gemini on rate-limit or error — the free-trial path (your own
    keys, shared/rate-limited across every trial user).

    With `provider_config` ({"api_key", "model", "base_url": optional}):
    builds a single OpenAICompatibleProvider as the ONLY provider, no
    fallback chain. This is a user's own key — silently falling back to the
    app's Groq/Gemini keys on failure would spend your quota on their
    behalf instead of surfacing their misconfiguration (bad key, wrong
    model name, provider outage) to them directly.
    """

    def __init__(self, system_prompt: str = "", provider_config: dict | None = None):
        self._system = system_prompt or _DEFAULT_SYSTEM
        self._primary   = None
        self._secondary = None
        self._active    = "none"
        self._is_byo    = bool(provider_config)
        if provider_config:
            self._init_byo(provider_config)
        else:
            self._init_trial()

    def _init_byo(self, provider_config: dict):
        from agents.providers.openai_compatible import OpenAICompatibleProvider
        self._primary = OpenAICompatibleProvider(
            self._system,
            api_key=provider_config.get("api_key", ""),
            model=provider_config.get("model", ""),
            base_url=provider_config.get("base_url"),
        )
        self._active = self._primary.name

    def _init_trial(self):
        PriCls, SecCls = (
            (_GroqProvider, _GeminiProvider)
            if PREFERRED_PROVIDER != "gemini"
            else (_GeminiProvider, _GroqProvider)
        )
        try:
            self._primary = PriCls(self._system)
            self._active  = self._primary.name
        except (ValueError, ImportError) as e:
            log.warning(f"Primary ({PriCls.name}) init failed: {e}")
        try:
            self._secondary = SecCls(self._system)
        except (ValueError, ImportError) as e:
            log.warning(f"Secondary ({SecCls.name}) init failed: {e}")

        if self._primary is None and self._secondary is None:
            raise ValueError(
                "No LLM provider configured.\n"
                "Add GROQ_API_KEY_1 or GEMINI_API_KEY to your .env file."
            )
        if self._primary is None:
            self._primary  = self._secondary
            self._secondary = None
            self._active   = self._primary.name

    @property
    def provider(self) -> str:
        return self._active

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        return self._call("ask", prompt, temperature, max_tokens, model)

    def chat(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None) -> str:
        return self._call("chat", prompt, temperature, max_tokens, model)

    def stream(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS, model: str | None = None):
        """
        Yields incremental text chunks. Falls back Groq -> Gemini only if a
        provider fails before yielding anything — once tokens are already on
        the wire, restarting on a different provider would duplicate output
        for the caller, so a mid-stream failure surfaces as a trailing note
        instead of a silent retry (same rate-limit/quota detection as _call,
        just no time.sleep() blocking the caller between attempts).
        """
        last_err = None
        for provider in [self._primary, self._secondary]:
            if provider is None:
                continue
            started = False
            try:
                for chunk in provider.stream(prompt, temperature, max_tokens, model):
                    started = True
                    self._active = provider.name
                    yield chunk
                return
            except Exception as e:
                last_err = e
                log.warning(f"[LLMClient] {provider.name} stream failed: {e}")
                if started:
                    yield f"\n\n⚠️ *Connection to {provider.name} dropped mid-response: {e}*"
                    return
        yield self._exhausted_message(last_err)

    def reset(self):
        if self._primary:   self._primary.reset()
        if self._secondary: self._secondary.reset()

    def load_history(self, messages: list[dict]):
        """Seeds conversation memory from a previously saved chat (resume-on-load)."""
        if self._primary:   self._primary.load_history(messages)
        if self._secondary: self._secondary.load_history(messages)

    def _call(self, method: str, prompt: str, temperature: float, max_tokens: int, model: str | None = None) -> str:
        last_err = None
        for attempt in range(MAX_RETRIES + 1):
            for provider in [self._primary, self._secondary]:
                if provider is None:
                    continue
                try:
                    result = getattr(provider, method)(prompt, temperature, max_tokens, model)
                    self._active = provider.name
                    return result
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    log.warning(f"[LLMClient] {provider.name} failed (attempt {attempt+1}): {e}")
                    if "429" in str(e) or "rate" in msg or "quota" in msg:
                        wait = min(4 ** attempt, 16)
                        log.info(f"[LLMClient] Rate-limited, waiting {wait}s before fallback")
                        time.sleep(wait)
        return self._exhausted_message(last_err)

    def _exhausted_message(self, last_err: Exception | None) -> str:
        """Every provider failed — the guidance differs by whether this
        client is on the trial path (your Groq/Gemini keys, likely just
        rate-limited) or a BYO key (their own provider, more likely a bad
        key/model/base_url than a rate limit — telling a BYO user to "wait
        for Gemini's daily quota to reset" would be actively wrong)."""
        if self._is_byo:
            return (
                f"⚠️ Your configured LLM provider failed.\n\n"
                f"Check your API key, model name, and base URL in Settings — "
                f"this is usually a bad key, an unavailable model, or a provider outage, "
                f"not a rate limit.\n\n"
                f"Last error: `{last_err}`"
            )
        return (
            f"⚠️ All LLM providers are currently rate-limited.\n\n"
            f"**What to do:**\n"
            f"- Groq: wait ~30 min for token quota to reset, or add more keys (GROQ_API_KEY_2, _3) to `.env`\n"
            f"- Gemini: wait for daily quota reset\n\n"
            f"Last error: `{last_err}`"
        )
