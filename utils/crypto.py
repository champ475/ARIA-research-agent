"""
utils/crypto.py — encrypt/decrypt user-supplied LLM API keys before they
touch Postgres (utils/db.py's user_llm_config table).

SECRET_KEY (≥ 32 random characters, validated at boot — SEC-11) is stretched
into a Fernet key via SHA-256 so operators don't have to generate a base64
Fernet key themselves.

Rotation (SEC-11): set the new value as SECRET_KEY and move the old one to
SECRET_KEY_PREVIOUS (comma-separated if several). `MultiFernet` encrypts with
the first key and decrypts with any, so stored keys keep working during the
window; `python scripts/rotate_secret_key.py` then re-encrypts every row with
the new key, after which SECRET_KEY_PREVIOUS can be removed.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

import config
from utils.errors import ConfigurationError

_fernet: MultiFernet | None = None


def _derive(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def _previous_secrets() -> list[str]:
    raw = getattr(config, "SECRET_KEY_PREVIOUS", "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is None:
        secret = config.SECRET_KEY
        if not secret:
            raise ConfigurationError(
                "Keys can't be stored securely on this server right now.",
                fix_hint="Ask the site operator to set SECRET_KEY.",
                detail="No SECRET_KEY found. Add SECRET_KEY=<32+ random characters> to .env.",
            )
        _fernet = MultiFernet([_derive(secret), *(_derive(s) for s in _previous_secrets())])
    return _fernet


def reset_cache() -> None:
    """Forget the derived keys (after SECRET_KEY / SECRET_KEY_PREVIOUS change)."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ConfigurationError(
            "Your saved API key can't be read any more.",
            fix_hint="Enter your key again in Settings.",
            detail="Stored API key could not be decrypted — SECRET_KEY may have changed.",
        ) from e


def rotate(ciphertext: str) -> str:
    """Re-encrypt `ciphertext` (readable by any configured key) with the
    current SECRET_KEY."""
    try:
        return _get_fernet().rotate(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ConfigurationError(
            detail="Ciphertext is not readable with SECRET_KEY or SECRET_KEY_PREVIOUS."
        ) from e
