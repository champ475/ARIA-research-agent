"""
agents/llm_client.py
Groq round-robin key rotation → Gemini fallback → optional local LLM.
Each LLMClient instance gets its own conversation history.
"""

import logging
import threading
import time

import prompts
from agents.conversation_memory import SUMMARY_PROMPT, ConversationMemory
from agents.providers.usage import ThreadLocalUsage, stream_event_usage
from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEYS,
    GROQ_MODEL,
    GROQ_MODEL_FAST,
    LLM_STREAM_TIMEOUT_SECONDS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    MAX_RETRIES,
    PROVIDER_ZERO_QUOTA_TTL_SECONDS,
    TOKENS_FAST,
    TOKENS_SYNTHESIS,
)
from utils import tracing
from utils.errors import ConfigurationError
from utils.tracing import usage_from_response

log = logging.getLogger(__name__)

# SEC-08: what a user sees when this server has no trial provider configured.
_NO_TRIAL_MESSAGE = "The free trial isn't available on this server right now."
_NO_TRIAL_HINT = "Add your own LLM API key in the Settings tab (any OpenAI-compatible provider works)."

# ── Thread-safe Groq key rotator ─────────────────────────────────────────────


class _KeyRotator:
    def __init__(self, keys: list[str]):
        self._keys = keys
        self._idx = 0
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


# ── Error classification & provider health ────────────────────────────────────
# Confirmed in production: Reflection/Verification prompts routinely 413'd
# against Groq free tier's TPM cap (a deterministic, prompt-size failure —
# retrying the identical prompt cannot succeed), while the fallback Gemini
# key reported a structurally zero quota (never provisioned, not "wait and
# retry"). Both used to be retried blindly, 3x2 attempts, every single call.

_TOO_LARGE_MARKERS = (
    "413",
    "too large",
    "request too large",
    "context length",
    "context_length_exceeded",
    "exceed_context_size",  # llama.cpp: "request (N tokens) exceeds the available context size"
    "exceeds the available context size",
)
_ZERO_QUOTA_MARKERS = ('"limit": "0"', "limit: 0", "limit=0")
_RATE_LIMIT_MARKERS = ("429", "rate", "quota")
# LLM-08: the provider no longer serves this model (retired / renamed).
_MODEL_GONE_MARKERS = (
    "decommissioned",
    "model_decommissioned",
    "model_not_found",
    "does not exist",
    "is not found",
    "not found for api version",
    "no longer supported",
    "has been deprecated",
)


def _classify_error(e: Exception) -> str:
    """Best-effort substring match on the stringified exception — deliberately
    biased toward false negatives (miss a case → same as pre-fix behavior,
    one wasted call) over false positives (skip a call that might have
    worked). Order matters: check the more specific "too_large"/"zero_quota"
    markers before the broad "rate_limited" ones, since a 413 message often
    also contains the word "rate" (Groq's TPM-limit wording)."""
    msg = str(e).lower()
    if any(m in msg for m in _TOO_LARGE_MARKERS):
        return "too_large"
    if any(m in msg for m in _ZERO_QUOTA_MARKERS):
        return "zero_quota"
    if any(m in msg for m in _MODEL_GONE_MARKERS):
        return "model_gone"
    if any(m in msg for m in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    return "other"


class _ProviderHealthCache:
    """Marks a provider unavailable for a TTL after it reports a
    structurally zero quota, so later calls skip its network round-trip
    entirely instead of re-attempting a call that cannot succeed until the
    quota is reprovisioned. Module-level, mirroring _groq_rotator above —
    the condition is a property of the configured API key, not any one
    LLMClient instance."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._unavailable_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark_unavailable(self, provider_name: str) -> None:
        with self._lock:
            self._unavailable_until[provider_name] = time.time() + self._ttl

    def is_unavailable(self, provider_name: str) -> bool:
        with self._lock:
            until = self._unavailable_until.get(provider_name)
            return until is not None and time.time() < until


_provider_health = _ProviderHealthCache(ttl_seconds=PROVIDER_ZERO_QUOTA_TTL_SECONDS)


def _hkey(provider: object) -> str:
    """Health is tracked per provider *and model* (LLM-08): a retired model
    must not take the provider's other models down with it."""
    key = getattr(provider, "health_key", None)
    return key if isinstance(key, str) and key else str(getattr(provider, "name", "?"))


class LLMProvidersExhausted(RuntimeError):
    """Raised when every configured provider failed to answer this call.
    Distinguishes "couldn't check" from any real LLM string output at the
    TYPE level, so callers — Reflection/Verification's JSON parsers
    especially — can no longer mistake total provider failure for a clean
    "0 issues found" response (which is exactly what used to happen when
    the old string-returning _exhausted_message() failed json.loads() and
    silently became an empty issues list)."""

    def __init__(self, last_err: Exception | None, is_byo: bool, too_large: bool = False):
        self.last_err = last_err
        self.is_byo = is_byo
        self.too_large = too_large
        super().__init__(str(last_err))


def _error_class(exc: LLMProvidersExhausted) -> str:
    """Coarse, user-safe description of why the provider failed. The raw
    upstream error is logged, never shown: for a BYO base_url it would echo
    whatever the endpoint returned (SEC-01), and provider errors can carry
    request ids, account names or key prefixes."""
    if exc.too_large:
        return "too_large"
    msg = str(exc.last_err or "").lower()
    if any(m in msg for m in _TOO_LARGE_MARKERS):
        return "too_large"
    if any(
        m in msg
        for m in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "incorrect api key",
            "api key not valid",
            "api_key_invalid",
            "authentication",
            "permission",
        )
    ):
        return "auth"
    if any(m in msg for m in ("404", "model_not_found", "does not exist", "not found")):
        return "model"
    if any(
        m in msg
        for m in (
            "connection",
            "timed out",
            "timeout",
            "resolve",
            "unreachable",
            "private or internal",
            "only https",
        )
    ):
        return "unreachable"
    if any(m in msg for m in _RATE_LIMIT_MARKERS) or any(m in msg for m in _ZERO_QUOTA_MARKERS):
        return "rate_limited"
    return "other"


_ERROR_CLASS_TEXT = {
    "too_large": "Your message (plus the research context) is too long for this model.",
    "auth": "The provider rejected the API key (authentication failed).",
    "model": "The provider doesn't recognise the configured model name.",
    "unreachable": "The provider couldn't be reached.",
    "rate_limited": "The provider is rate-limiting requests right now.",
    "other": "The provider returned an unexpected error.",
}


def format_exhausted_message(exc: LLMProvidersExhausted) -> str:
    """Free function (no LLMClient instance required) so callers outside
    LLMClient — agents/graph_orchestrator.py's top-level catch — can format
    a user-facing message from a caught LLMProvidersExhausted. The guidance
    differs by whether the client was on the trial path (your Groq/Gemini
    keys, likely just rate-limited) or a BYO key (their own provider, more
    likely a bad key/model/base_url than a rate limit — telling a BYO user
    to "wait for Gemini's daily quota to reset" would be actively wrong)."""
    kind = _error_class(exc)
    log.warning(f"[LLMClient] providers exhausted (byo={exc.is_byo}, kind={kind}): {exc.last_err}")
    if kind == "too_large":
        return (
            "⚠️ That request is too long for the model.\n\n"
            "Try a shorter message, or ask about fewer companies at once."
        )
    if exc.is_byo and kind == "auth":
        # UX-02: the fix, not just the fault.
        return "⚠️ Your API key was rejected by your provider. Update it in **⚙️ Settings**, then ask again."
    if exc.is_byo:
        return (
            f"⚠️ Your configured LLM provider failed: {_ERROR_CLASS_TEXT[kind]}\n\n"
            f"Check your API key, model name, and base URL in Settings — "
            f"this is usually a bad key, an unavailable model, or a provider outage, "
            f"not a rate limit."
        )
    if kind != "rate_limited":
        # Smoke test 2026-10-14: a decommissioned model / invalid trial key was
        # reported to users as "rate-limited, wait 30 min" — wrong advice.
        return (
            "⚠️ The shared AI service isn't available right now "
            f"({_ERROR_CLASS_TEXT[kind].rstrip('.').lower()}).\n\n"
            "Please try again later, or add your own LLM key in **Settings** to keep going."
        )
    return (
        "⚠️ All LLM providers are currently rate-limited.\n\n"
        "**What to do:**\n"
        "- Groq: wait ~30 min for token quota to reset, or add more keys (GROQ_API_KEY_2, _3) to `.env`\n"
        "- Gemini: wait for daily quota reset"
    )


# ── Provider wrappers ─────────────────────────────────────────────────────────


class _GroqProvider(ThreadLocalUsage):
    name = "Groq"

    def __init__(self, system: str, model: str | None = None):
        if not _groq_rotator.available():
            raise ConfigurationError(
                _NO_TRIAL_MESSAGE,
                fix_hint=_NO_TRIAL_HINT,
                detail="No GROQ_API_KEY found: add GROQ_API_KEY_1 to .env (free at console.groq.com).",
            )
        try:
            from groq import Groq as _Groq

            self._Groq = _Groq
        except ImportError as e:
            raise ImportError("Run: pip install groq") from e
        self.model = model or GROQ_MODEL
        self.health_key = f"groq:{self.model}"
        self._system = system
        self._memory = ConversationMemory(cap=20, summarizer=self._summarize)

    def _client(self):
        # REL-02: explicit timeout (the SDK default is 60 s), and no SDK-level
        # retries — LLMClient._call owns retry/fallback, so SDK retries would
        # multiply every slow failure.
        return self._Groq(api_key=_groq_rotator.next(), timeout=LLM_TIMEOUT_SECONDS, max_retries=0)

    def _summarize(self, old_summary: str, chunk_text: str) -> str:
        prompt = SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        r = self._client().chat.completions.create(
            model=GROQ_MODEL_FAST,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=TOKENS_FAST,
        )
        self.last_usage = usage_from_response(r)
        return r.choices[0].message.content.strip()

    def ask(
        self,
        prompt: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        msgs = [{"role": "system", "content": self._system}, {"role": "user", "content": prompt}]
        r = self._client().chat.completions.create(
            model=model or self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.last_usage = usage_from_response(r)
        return r.choices[0].message.content.strip()

    def chat(
        self,
        msg: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        r = self._client().chat.completions.create(
            model=model or self.model,
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.last_usage = usage_from_response(r)
        reply = r.choices[0].message.content.strip()
        self._memory.add_assistant(reply)
        return reply

    def stream(
        self,
        msg: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ):
        self._memory.add_user(msg)
        msgs = [{"role": "system", "content": self._system}] + self._memory.context()
        chunks: list[str] = []
        try:
            events = self._client().chat.completions.create(
                model=model or self.model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                # Longest gap between chunks, so also the time-to-first-token cap.
                timeout=LLM_STREAM_TIMEOUT_SECONDS,
            )
            for event in events:
                usage = stream_event_usage(event)
                if usage is not None:
                    self.last_usage = usage
                delta = event.choices[0].delta.content if event.choices else None
                if delta:
                    chunks.append(delta)
                    yield delta
        except Exception:
            if not chunks:
                self._memory.pop_last()  # never produced output — don't leave an orphan turn
            raise
        self._memory.add_assistant("".join(chunks))

    def reset(self):
        self._memory.clear()

    def load_history(self, messages: list[dict]):
        self._memory.load(messages)


class _GeminiProvider(ThreadLocalUsage):
    name = "Gemini"

    def __init__(self, system: str, model: str | None = None):
        if not GEMINI_API_KEY:
            raise ConfigurationError(
                _NO_TRIAL_MESSAGE,
                fix_hint=_NO_TRIAL_HINT,
                detail="No GEMINI_API_KEY found: add it to .env (free at aistudio.google.com/apikey).",
            )
        try:
            from google import genai
            from google.genai import types as gt

            # REL-02: HttpOptions.timeout is in milliseconds.
            self._client = genai.Client(
                api_key=GEMINI_API_KEY,
                http_options=gt.HttpOptions(timeout=int(LLM_TIMEOUT_SECONDS * 1000)),
            )
            self._types = gt
        except ImportError as e:
            raise ImportError("Run: pip install google-genai") from e
        self.model = model or GEMINI_MODEL
        self.health_key = f"gemini:{self.model}"
        self._system = system
        self._memory = ConversationMemory(cap=20, summarizer=self._summarize)

    def _summarize(self, old_summary: str, chunk_text: str) -> str:
        prompt = SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        cfg = self._types.GenerateContentConfig(temperature=0.2, max_output_tokens=TOKENS_FAST)
        r = self._client.models.generate_content(model=self.model, contents=prompt, config=cfg)
        self.last_usage = usage_from_response(r)
        return r.text.strip()

    def ask(
        self,
        prompt: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        # model override is a Groq-only concept (fast vs primary model) — Gemini
        # is already the fallback tier, so it always uses its configured model.
        cfg = self._types.GenerateContentConfig(
            system_instruction=self._system,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=cfg,
        )
        self.last_usage = usage_from_response(r)
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

    def chat(
        self,
        msg: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        self._memory.add_user(msg)
        system, contents = self._system_and_contents()
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        r = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=cfg,
        )
        self.last_usage = usage_from_response(r)
        reply = r.text.strip()
        self._memory.add_assistant(reply)
        return reply

    def stream(
        self,
        msg: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ):
        self._memory.add_user(msg)
        system, contents = self._system_and_contents()
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        chunks: list[str] = []
        try:
            for event in self._client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=cfg,
            ):
                usage = stream_event_usage(event)
                if usage is not None:
                    self.last_usage = usage
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

_DEFAULT_SYSTEM = prompts.get("llm.default_system")


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

    Either way, when LOCAL_LLM_BASE_URL is set, a local model is the last
    tier (agents/providers/local_llm.py). It spends nobody's quota, so it
    also backs BYO keys; the Settings "Test connection" button builds the
    BYO provider directly, so a bad key is still reported there.
    """

    _local = None  # class default: tests build bare instances via __new__
    _prompt_version = ""
    _extra: tuple | list = ()  # route entries after primary/secondary (LLM-08)
    _node: str | None = None

    def _observe(self, provider, model: str | None, tokens_in: int, tokens_out: int, cost: float) -> None:
        """OBS-03 counters; a call served by other than the first choice is a fallback."""
        from utils import metrics

        try:
            first = self._chain()[0]
        except Exception:
            first = provider
        metrics.observe_llm_call(
            self._node, str(provider.name), model, tokens_in, tokens_out, cost, provider is not first
        )

    def _record_llm_call(
        self, provider, prompt: str, output: str, streamed: bool = False, ms: int = 0
    ) -> None:
        """OBS-02/LLM-11: count this call on the current turn's record —
        tokens from the provider's usage report (also for streams that sent
        one), else estimated; plus node, model, latency and rupee cost."""
        rec = tracing.current()
        usage = getattr(provider, "last_usage", None)
        if isinstance(usage, tuple):
            tokens_in, tokens_out, estimated = usage[0], usage[1], False
        else:
            tokens_in = tracing.estimate_tokens(getattr(self, "_system", "") + prompt)
            tokens_out, estimated = tracing.estimate_tokens(output), True
        from config import call_cost_inr

        model = getattr(provider, "model", None)
        model = model if isinstance(model, str) else None
        if rec is None:
            # Outside a turn (startup probe, worker job): metrics only.
            self._observe(provider, model, tokens_in, tokens_out, 0.0)
            return
        cost = 0.0 if self._is_byo else call_cost_inr(model, tokens_in, tokens_out)
        self._observe(provider, model, tokens_in, tokens_out, cost)
        rec.add_llm_call(
            str(provider.name),
            self._prompt_version,
            tokens_in,
            tokens_out,
            estimated,
            node=self._node,
            model=model,
            ms=ms,
            cost_inr=cost,
        )

    def __init__(self, system_prompt: str = "", provider_config: dict | None = None, node: str | None = None):
        # LLM-08: `node` picks the model route (config.model_route) on the
        # trial path; a BYO user's own configured model is always used.
        self._node = node
        self._extra = []
        self._system = system_prompt or _DEFAULT_SYSTEM
        # LLM-07: a registry prompt records its id ("planner.system@v1");
        # anything else (ad-hoc system text) falls back to a content hash.
        self._prompt_version = getattr(self._system, "id", None) or tracing.prompt_version(self._system)
        self._primary = None
        self._secondary = None
        self._active = "none"
        self._is_byo = bool(provider_config)
        self._local = self._init_local()
        if provider_config:
            self._init_byo(provider_config)
        else:
            self._init_trial()

    def _init_local(self):
        from agents.providers.local_llm import LocalLLMProvider, local_llm_configured

        if not local_llm_configured():
            return None
        try:
            return LocalLLMProvider(self._system)
        except (ValueError, ImportError) as e:
            log.warning(f"Local LLM init failed: {e}")
            return None

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
        """Providers in the node's route order (LLM-08), skipping models
        already known to be unavailable and providers without a key."""
        from config import model_route

        classes = {"groq": _GroqProvider, "gemini": _GeminiProvider}
        chain: list = []
        for provider_name, model in model_route(self._node):
            if _provider_health.is_unavailable(f"{provider_name}:{model}"):
                continue
            try:
                chain.append(classes[provider_name](self._system, model=model))
            except (ValueError, ImportError) as e:
                log.debug("trial provider %s:%s not usable: %s", provider_name, model, type(e).__name__)
        if not chain:
            if self._local is not None:
                self._active = self._local.name
                return
            raise ConfigurationError(
                _NO_TRIAL_MESSAGE,
                fix_hint=_NO_TRIAL_HINT,
                detail="No LLM provider configured: add GROQ_API_KEY_1 or GEMINI_API_KEY to .env.",
            )
        self._primary = chain[0]
        self._secondary = chain[1] if len(chain) > 1 else None
        self._extra = chain[2:]
        self._active = self._primary.name

    def _chain(self) -> list:
        """Providers in fallback order: primary, secondary, then local."""
        return [p for p in (self._primary, self._secondary, *self._extra, self._local) if p is not None]

    @property
    def provider(self) -> str:
        return self._active

    def ask(
        self,
        prompt: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        return self._call("ask", prompt, temperature, max_tokens, model)

    def chat(
        self,
        prompt: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ) -> str:
        return self._call("chat", prompt, temperature, max_tokens, model)

    def stream(
        self,
        prompt: str,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int = TOKENS_SYNTHESIS,
        model: str | None = None,
    ):
        """
        Yields incremental text chunks. Falls back Groq -> Gemini -> local only if a
        provider fails before yielding anything — once tokens are already on
        the wire, restarting on a different provider would duplicate output
        for the caller, so a mid-stream failure surfaces as a trailing note
        instead of a silent retry (same rate-limit/quota detection as _call,
        just no time.sleep() blocking the caller between attempts).
        """
        last_err = None
        attempted = too_large = 0
        for provider in self._chain():
            if _provider_health.is_unavailable(_hkey(provider)):
                continue
            attempted += 1
            started = False
            streamed: list[str] = []
            t0 = time.monotonic()
            if hasattr(provider, "last_usage"):
                provider.last_usage = None
            try:
                with tracing.span(
                    f"llm {provider.name}",
                    "llm",
                    inputs={"prompt": prompt},
                    metadata={"model": model or "", "prompt_version": self._prompt_version, "stream": True},
                ) as span_out:
                    for chunk in provider.stream(prompt, temperature, max_tokens, model):
                        started = True
                        self._active = provider.name
                        streamed.append(chunk)
                        yield chunk
                    span_out["output"] = "".join(streamed)
                self._record_llm_call(
                    provider, prompt, "".join(streamed), streamed=True, ms=int((time.monotonic() - t0) * 1000)
                )
                return
            except Exception as e:
                last_err = e
                kind = _classify_error(e)
                log.warning(f"[LLMClient] {provider.name} stream failed ({kind}): {e}")
                if kind in ("zero_quota", "model_gone"):
                    _provider_health.mark_unavailable(_hkey(provider))
                elif kind == "too_large":
                    too_large += 1
                if started:
                    # The raw error is logged above, never shown (SEC-01).
                    yield "\n\n⚠️ *The connection to the model dropped mid-response — please ask again.*"
                    return
        # VAL-01: an oversized prompt must surface as "too long", not as a rate limit.
        raise LLMProvidersExhausted(
            last_err, is_byo=self._is_byo, too_large=attempted > 0 and too_large == attempted
        )

    def reset(self):
        for provider in self._chain():
            provider.reset()

    def load_history(self, messages: list[dict]):
        """Seeds conversation memory from a previously saved chat (resume-on-load)."""
        for provider in self._chain():
            provider.load_history(messages)

    def _call(
        self, method: str, prompt: str, temperature: float, max_tokens: int, model: str | None = None
    ) -> str:
        last_err = None
        too_large_providers: set[str] = set()
        local_tried = False
        providers = self._chain()
        for attempt in range(MAX_RETRIES + 1):
            for provider in providers:
                if provider.name in too_large_providers:
                    continue
                if provider is self._local:
                    # At most once per call: it is down (refused), hung (timeout)
                    # or the prompt doesn't fit — none of which a retry fixes.
                    if local_tried:
                        continue
                    local_tried = True
                if _provider_health.is_unavailable(_hkey(provider)):
                    continue
                try:
                    t0 = time.monotonic()
                    with tracing.span(
                        f"llm {provider.name}",
                        "llm",
                        inputs={"prompt": prompt},
                        metadata={"model": model or "", "prompt_version": self._prompt_version},
                    ) as span_out:
                        result = getattr(provider, method)(prompt, temperature, max_tokens, model)
                        span_out["output"] = result
                    self._active = provider.name
                    self._record_llm_call(provider, prompt, result, ms=int((time.monotonic() - t0) * 1000))
                    return result
                except Exception as e:
                    last_err = e
                    kind = _classify_error(e)
                    log.warning(f"[LLMClient] {provider.name} failed (attempt {attempt + 1}, {kind}): {e}")
                    if kind == "rate_limited":
                        wait = min(4**attempt, 16)
                        log.info(f"[LLMClient] Rate-limited, waiting {wait}s before fallback")
                        time.sleep(wait)
                    elif kind == "too_large":
                        # Retrying an identical oversized prompt against the
                        # SAME provider cannot succeed — a 413 here means
                        # the prompt itself needs to shrink, not that the
                        # provider needs another chance. Never sleep, never
                        # retry this provider again within this call.
                        too_large_providers.add(provider.name)
                    elif kind in ("zero_quota", "model_gone"):
                        # LLM-08: a retired model or a never-provisioned quota
                        # won't recover within this call — skip it from now on.
                        _provider_health.mark_unavailable(_hkey(provider))
            if providers and too_large_providers >= {p.name for p in providers}:
                break  # every provider already rejected this exact prompt — stop burning the retry budget
        raise LLMProvidersExhausted(last_err, is_byo=self._is_byo, too_large=bool(too_large_providers))
