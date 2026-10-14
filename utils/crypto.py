"""
utils/crypto.py — encrypt/decrypt user-supplied LLM API keys before they
touch Postgres (utils/db.py's user_llm_config table). SECRET_KEY (any
string, set once, never rotated without losing access to stored keys) is
stretched into a valid Fernet key via SHA-256 so users don't have to
generate/paste a base64 Fernet key themselves.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

import config

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        secret = config.SECRET_KEY
        if not secret:
            raise ValueError(
                "No SECRET_KEY found.\n"
                "Add SECRET_KEY=<any random string> to your .env — used to encrypt "
                "user-supplied LLM API keys before they're stored."
            )
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Stored API key could not be decrypted — SECRET_KEY may have changed.")
