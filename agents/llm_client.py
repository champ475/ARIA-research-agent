"""
agents/llm_client.py
Groq round-robin key rotation → Gemini fallback.
Each LLMClient instance gets its own conversation history.
"""

import time
import logging
import threading
from config import (
    GROQ_API_KEYS, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL,
    PREFERRED_PROVIDER, MAX_RETRIES,
    TOKENS_SYNTHESIS, LLM_TEMPERATURE,
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
        self._history: list[dict] = []

    def _client(self):
        return self._Groq(api_key=_groq_rotator.next())

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        msgs = [{"role": "system", "content": self._system},
                {"role": "user",   "content": prompt}]
        r = self._client().chat.completions.create(
            model=GROQ_MODEL, messages=msgs,
            temperature=temperature, max_tokens=max_tokens,
        )
        return r.choices[0].message.content.strip()

    def chat(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        self._history.append({"role": "user", "content": msg})
        msgs = [{"role": "system", "content": self._system}] + self._history[-20:]
        r = self._client().chat.completions.create(
            model=GROQ_MODEL, messages=msgs,
            temperature=temperature, max_tokens=max_tokens,
        )
        reply = r.choices[0].message.content.strip()
        self._history.append({"role": "assistant", "content": reply})
        return reply

    def stream(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS):
        self._history.append({"role": "user", "content": msg})
        msgs = [{"role": "system", "content": self._system}] + self._history[-20:]
        chunks: list[str] = []
        try:
            events = self._client().chat.completions.create(
                model=GROQ_MODEL, messages=msgs,
                temperature=temperature, max_tokens=max_tokens, stream=True,
            )
            for event in events:
                delta = event.choices[0].delta.content
                if delta:
                    chunks.append(delta)
                    yield delta
        except Exception:
            if not chunks:
                self._history.pop()   # never produced output — don't leave an orphan turn
            raise
        self._history.append({"role": "assistant", "content": "".join(chunks)})

    def reset(self):
        self._history.clear()

    def load_history(self, messages: list[dict]):
        self._history = list(messages[-20:])


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
        self._history: list[dict] = []

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        cfg = self._types.GenerateContentConfig(
            system_instruction=self._system,
            temperature=temperature, max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=cfg,
        )
        return r.text.strip()

    def chat(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        self._history.append({"role": "user", "content": msg})
        contents = [
            self._types.Content(
                role="user" if t["role"] == "user" else "model",
                parts=[self._types.Part(text=t["content"])],
            )
            for t in self._history[-20:]
        ]
        cfg = self._types.GenerateContentConfig(
            system_instruction=self._system,
            temperature=temperature, max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=cfg,
        )
        reply = r.text.strip()
        self._history.append({"role": "assistant", "content": reply})
        return reply

    def stream(self, msg: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS):
        self._history.append({"role": "user", "content": msg})
        contents = [
            self._types.Content(
                role="user" if t["role"] == "user" else "model",
                parts=[self._types.Part(text=t["content"])],
            )
            for t in self._history[-20:]
        ]
        cfg = self._types.GenerateContentConfig(
            system_instruction=self._system,
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
                self._history.pop()
            raise
        self._history.append({"role": "assistant", "content": "".join(chunks)})

    def reset(self):
        self._history.clear()

    def load_history(self, messages: list[dict]):
        self._history = list(messages[-20:])


# ── Unified client ────────────────────────────────────────────────────────────

_DEFAULT_SYSTEM = (
    "You are ARIA, an expert institutional equity research analyst for Indian markets (NSE/BSE). "
    "Always respond with precise, data-grounded analysis. Format in clean Markdown."
)


class LLMClient:
    """
    Tries Groq (with key rotation) first.
    On rate-limit or error, falls back to Gemini.
    """

    def __init__(self, system_prompt: str = ""):
        self._system = system_prompt or _DEFAULT_SYSTEM
        self._primary   = None
        self._secondary = None
        self._active    = "none"
        self._init()

    def _init(self):
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

    def ask(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        return self._call("ask", prompt, temperature, max_tokens)

    def chat(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS) -> str:
        return self._call("chat", prompt, temperature, max_tokens)

    def stream(self, prompt: str, temperature: float = LLM_TEMPERATURE, max_tokens: int = TOKENS_SYNTHESIS):
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
                for chunk in provider.stream(prompt, temperature, max_tokens):
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
        yield (
            f"⚠️ All LLM providers are currently rate-limited.\n\n"
            f"**What to do:**\n"
            f"- Groq: wait ~30 min for token quota to reset, or add more keys (GROQ_API_KEY_2, _3) to `.env`\n"
            f"- Gemini: wait for daily quota reset\n\n"
            f"Last error: `{last_err}`"
        )

    def reset(self):
        if self._primary:   self._primary.reset()
        if self._secondary: self._secondary.reset()

    def load_history(self, messages: list[dict]):
        """Seeds conversation memory from a previously saved chat (resume-on-load)."""
        if self._primary:   self._primary.load_history(messages)
        if self._secondary: self._secondary.load_history(messages)

    def _call(self, method: str, prompt: str, temperature: float, max_tokens: int) -> str:
        last_err = None
        for attempt in range(MAX_RETRIES + 1):
            for provider in [self._primary, self._secondary]:
                if provider is None:
                    continue
                try:
                    result = getattr(provider, method)(prompt, temperature, max_tokens)
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
        return (
            f"⚠️ All LLM providers are currently rate-limited.\n\n"
            f"**What to do:**\n"
            f"- Groq: wait ~30 min for token quota to reset, or add more keys (GROQ_API_KEY_2, _3) to `.env`\n"
            f"- Gemini: wait for daily quota reset\n\n"
            f"Last error: `{last_err}`"
        )
