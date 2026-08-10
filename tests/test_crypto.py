"""utils/crypto.py — encrypt/decrypt round-trip and SECRET_KEY failure modes.

_fernet is cached module-level (see utils/crypto.py), so every test resets
it via monkeypatch to avoid leaking state between cases.
"""
import pytest

import config
import utils.crypto as crypto


@pytest.fixture(autouse=True)
def _reset_fernet_cache(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", None)
    yield
    monkeypatch.setattr(crypto, "_fernet", None)


def test_get_fernet_raises_when_secret_key_empty(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    with pytest.raises(ValueError, match="No SECRET_KEY found"):
        crypto._get_fernet()


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-key-12345")
    plaintext = "sk-example-api-key-value"
    ciphertext = crypto.encrypt(plaintext)
    assert ciphertext != plaintext
    assert crypto.decrypt(ciphertext) == plaintext


def test_decrypt_under_different_key_raises(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "key-one")
    ciphertext = crypto.encrypt("some-api-key")

    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(config, "SECRET_KEY", "key-two")
    with pytest.raises(ValueError, match="could not be decrypted"):
        crypto.decrypt(ciphertext)


def test_encrypt_raises_when_secret_key_empty(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    with pytest.raises(ValueError, match="No SECRET_KEY found"):
        crypto.encrypt("some-api-key")
