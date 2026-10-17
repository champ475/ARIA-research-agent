"""SEC-07: length is checked before hashing, weak passwords are rejected,
argon2id is the hash, and legacy PBKDF2 hashes still verify."""

from __future__ import annotations

import pytest

from utils import auth


def test_oversize_password_is_rejected_before_hashing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("hashed an oversize password")

    monkeypatch.setattr(auth, "_hash_password", boom)
    with pytest.raises(auth.AuthError):
        auth.check_new_password("x" * 129)


@pytest.mark.parametrize("pw", ["password123", "Password123", "12345678", "aaaaaaaaaa", "abababab"])
def test_weak_passwords_rejected(pw):
    with pytest.raises(auth.AuthError):
        auth.check_new_password(pw)


def test_password_equal_to_email_local_part_rejected():
    with pytest.raises(auth.AuthError):
        auth.check_new_password("arnavmendiratta", "arnavmendiratta@example.com")


def test_strong_password_accepted():
    auth.check_new_password("correct-horse-battery-staple")


def test_hash_is_argon2id_and_verifies():
    h = auth._hash_password("correct-horse-battery-staple")
    assert h.startswith("$argon2id$")
    assert auth._verify_password("correct-horse-battery-staple", h)
    assert not auth._verify_password("wrong", h)
    assert not auth._needs_rehash(h)


def test_legacy_pbkdf2_hash_verifies_and_needs_rehash():
    legacy = auth._hash_pbkdf2("old-password-1", b"\x01" * 16)
    assert auth._verify_password("old-password-1", legacy)
    assert not auth._verify_password("nope", legacy)
    assert auth._needs_rehash(legacy)


def test_garbage_hash_never_verifies():
    assert not auth._verify_password("x", "not-a-hash")
    assert not auth._verify_password("x", "$argon2id$garbage")
