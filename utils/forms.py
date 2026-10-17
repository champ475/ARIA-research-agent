"""
utils/forms.py — validation for the Watchlist and Settings forms (VAL-07).

Every form value is checked before it's saved, with a message that says
what to fix: watchlist symbols must resolve to a listed NSE company (via the
VAL-02 instrument master), and a bring-your-own LLM key is saved only after
it answers a one-token test call.
"""

from __future__ import annotations

import re

from utils.errors import ConfigurationError, UserInputError

WATCHLIST_MAX = 50
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")
MODEL_MAX_LEN = 100
KEY_MAX_LEN = 300
TEST_CALL_TIMEOUT_SECONDS = 10.0


def watchlist_symbol(raw: str | None) -> str:
    """The listed NSE symbol a watchlist entry refers to, or UserInputError
    with up to three suggestions."""
    text = " ".join((raw or "").split())
    if not text:
        raise UserInputError("Enter a company name or NSE symbol.")
    if len(text) > 60:
        raise UserInputError("That's too long for a company name or symbol.")
    from tools.data.instruments import resolve

    res = resolve(text)
    if res.status == "found" and res.symbol and SYMBOL_RE.match(res.symbol):
        return res.symbol
    options = [c.symbol for c in res.candidates[:3]]
    if res.status == "ambiguous" and options:
        raise UserInputError(f"“{text}” could be {', '.join(options)} — enter the exact NSE symbol.")
    hint = f" Did you mean {', '.join(options)}?" if options else ""
    raise UserInputError(f"Couldn't find a listed NSE company called “{text}”.{hint}")


def llm_settings(model: str | None, api_key: str | None, base_url: str | None) -> tuple[str, str, str | None]:
    """Clean and check a BYO LLM configuration's format (not its liveness)."""
    model = (model or "").strip()
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip() or None
    if not model:
        raise UserInputError("Enter a model name (e.g. gpt-4o-mini, llama-3.3-70b-versatile).")
    if len(model) > MODEL_MAX_LEN or any(c.isspace() for c in model):
        raise UserInputError(f"The model name must be one word of at most {MODEL_MAX_LEN} characters.")
    if not api_key:
        raise UserInputError("Enter your API key.")
    if len(api_key) > KEY_MAX_LEN or any(c.isspace() for c in api_key):
        raise UserInputError(
            f"That doesn't look like an API key (no spaces, at most {KEY_MAX_LEN} characters)."
        )
    if base_url:
        from utils.url_safety import validate_public_https_url

        base_url = validate_public_https_url(base_url)  # SEC-01; raises UnsafeURLError (a UserInputError)
    return model, api_key, base_url


def probe_llm(model: str, api_key: str, base_url: str | None) -> None:
    """One 1-token call, at most TEST_CALL_TIMEOUT_SECONDS. Raises
    ConfigurationError with a coarse reason (never the provider's raw reply,
    SEC-01) if the provider doesn't answer."""
    import config

    if config.ARIA_FAKE_LLM:  # TEST-04: e2e runs have no real provider to call
        return
    from agents.llm_client import _ERROR_CLASS_TEXT, LLMProvidersExhausted, _error_class
    from agents.providers.openai_compatible import OpenAICompatibleProvider

    class _Probe(OpenAICompatibleProvider):
        _request_timeout = TEST_CALL_TIMEOUT_SECONDS

    try:
        _Probe("Reply with OK.", api_key, model, base_url).ask("ping", temperature=0.0, max_tokens=1)
    except UserInputError:
        raise
    except Exception as e:
        kind = _error_class(LLMProvidersExhausted(e, is_byo=True))
        raise ConfigurationError(
            f"The provider didn't accept this configuration: {_ERROR_CLASS_TEXT[kind]}",
            fix_hint="Check the model name, key and base URL, then save again.",
            detail=f"probe failed ({kind}): {type(e).__name__}",
        ) from e
