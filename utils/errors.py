"""
utils/errors.py — typed, user-safe errors (SEC-08).

Users see a friendly message plus a short error ID; the exception text, stack
and any SQL / provider / path detail go only to the logs. Code that knows what
went wrong raises one of these with a `user_message` it wrote on purpose;
everything else is an unknown failure and gets the generic message.

    UserInputError        the request itself can't be done as asked (shown as-is)
    ConfigurationError    a setting must change: a BYO key, the operator's .env
    DataUnavailableError  a market-data source is down or empty
    ProviderError         the LLM providers failed
    InternalError         our bug; generic message + error ID

`render_error()` is the one place a caught exception becomes user text.
`fix_hint` says what the user can do about it (UX-02 links it to Settings).
"""

from __future__ import annotations

GENERIC_MESSAGE = "Something went wrong on our side while answering that."


class AriaError(Exception):
    """Base class: an error whose `user_message` is safe to show."""

    default_message = GENERIC_MESSAGE
    show_error_id = True

    def __init__(
        self, user_message: str | None = None, *, fix_hint: str | None = None, detail: str | None = None
    ):
        self.user_message = user_message or self.default_message
        self.fix_hint = fix_hint
        # `detail` is for logs only; str(exc) carries it so log.exception shows it.
        super().__init__(detail or self.user_message)


class UserInputError(AriaError, ValueError):
    """The request can't be done as asked. The message says why; no error ID
    (nothing went wrong on our side)."""

    default_message = "That request couldn't be processed as written."
    show_error_id = False


class ConfigurationError(AriaError, ValueError):
    """A setting must change before this works (BYO key, model, base URL, or
    the operator's environment)."""

    default_message = "ARIA isn't configured to answer that right now."
    show_error_id = False


class DataUnavailableError(AriaError):
    default_message = "The market data needed for that isn't available right now. Please try again shortly."


# DATA-02: how each data source is named to users when it's down.
SOURCE_NAMES = {"nse": "NSE", "bse": "BSE", "yfinance": "Market quote", "amfi": "AMFI fund", "news": "News"}


class SourceUnavailable(DataUnavailableError):
    """A data source refused or failed the request (blocked, 5xx, timeout,
    malformed reply) — as opposed to answering with no rows."""

    def __init__(self, source: str, detail: str | None = None):
        self.source = source
        label = SOURCE_NAMES.get(source, source)
        super().__init__(f"{label} data is temporarily unavailable.", detail=detail)


class ProviderError(AriaError):
    default_message = "The AI model couldn't be reached right now. Please try again in a minute."


class InternalError(AriaError):
    pass


def error_id_from_turn(turn_id: str | None) -> str:
    """The short ID users report (the turn id prefix — scripts/find_turn.py)."""
    return (turn_id or "").replace("-", "")[:8] or "unknown"


def render_error(exc: BaseException | None, error_id: str) -> str:
    """User-facing Markdown for a caught exception. Never includes the
    exception's own text unless it is an AriaError's `user_message`."""
    if isinstance(exc, AriaError):
        text = f"⚠️ {exc.user_message}"
        if exc.fix_hint:
            text += f"\n\n**What to do:** {exc.fix_hint}"
        if exc.show_error_id:
            text += f"\n\n_Ref: `{error_id}`_"
        return text
    return (
        f"⚠️ {GENERIC_MESSAGE}\n\nPlease try again. If it keeps happening, report **Error ID `{error_id}`**."
    )
