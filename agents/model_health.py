"""
agents/model_health.py — check every routed model at startup (LLM-08).

One 1-token call per distinct (provider, model) in config.model_route. A
model the provider has retired (or a quota that was never provisioned) is
logged as a warning and marked unavailable, so LLMClient routes straight to
the next entry instead of discovering it on a user's turn. Runs in a
background thread: start-up never waits on the providers.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


def check_models() -> dict[str, str]:
    """{"groq:openai/gpt-oss-20b": "ok" | "no_key" | "model_gone" | ...}."""
    from agents.llm_client import _classify_error, _GeminiProvider, _GroqProvider, _provider_health
    from config import all_routed_models
    from utils.errors import ConfigurationError

    classes = {"groq": _GroqProvider, "gemini": _GeminiProvider}
    results: dict[str, str] = {}
    for provider_name, model in all_routed_models():
        key = f"{provider_name}:{model}"
        try:
            provider: Any = classes[provider_name]("Reply with OK.", model=model)
            provider.ask("ping", 0.0, 1, None)
            results[key] = "ok"
        except ConfigurationError:
            results[key] = "no_key"
        except Exception as e:
            kind = _classify_error(e)
            results[key] = kind
            if kind in ("model_gone", "zero_quota"):
                _provider_health.mark_unavailable(key)
    usable = [k for k, v in results.items() if v == "ok"]
    unusable = {k: v for k, v in results.items() if v not in ("ok", "no_key")}
    if unusable:
        log.warning("[model_health] models not usable, routed around: %s", unusable)
    log.info("[model_health] available models: %s", usable or "none")
    return results


def start_model_health_check() -> threading.Thread | None:
    import config

    if config.APP_ENV == "test":
        return None
    t = threading.Thread(target=check_models, name="aria-model-health", daemon=True)
    t.start()
    return t
