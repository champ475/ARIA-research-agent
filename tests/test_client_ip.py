"""SEC-05: X-Forwarded-For is honoured only from configured trusted proxies."""

from __future__ import annotations

import pytest
from starlette.requests import Request

import config
from web.session import client_ip


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers, "method": "GET", "path": "/"})


@pytest.fixture
def proxies(monkeypatch):
    def use(*entries: str) -> None:
        monkeypatch.setattr(config, "TRUSTED_PROXIES", list(entries))

    return use


def test_header_ignored_from_an_untrusted_peer(proxies):
    proxies()
    assert client_ip(_request("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_header_used_behind_a_trusted_proxy(proxies):
    proxies("10.0.0.0/8")
    assert client_ip(_request("10.1.2.3", "198.51.100.7")) == "198.51.100.7"


def test_spoofed_left_entries_are_ignored(proxies):
    """A client can prepend anything; only the address our proxy appended counts."""
    proxies("10.0.0.0/8", "172.16.0.5")
    assert client_ip(_request("10.1.2.3", "6.6.6.6, 198.51.100.7, 172.16.0.5")) == "198.51.100.7"


def test_all_hops_trusted_falls_back_to_peer(proxies):
    proxies("10.0.0.0/8")
    assert client_ip(_request("10.1.2.3", "10.9.9.9")) == "10.1.2.3"
