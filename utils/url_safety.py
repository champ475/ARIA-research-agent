"""
utils/url_safety.py — outbound URL policy for user-supplied endpoints (SEC-01).

A bring-your-own LLM `base_url` makes the server send requests wherever a user
points it. Without a check, `http://169.254.169.254/...` (cloud metadata),
`http://localhost:5432` or any internal host becomes a request-forgery and
port-scanning tool run from inside the deployment.

Policy: only `https` URLs whose host resolves exclusively to public (globally
routable) addresses. Checked when the URL is saved, when a saved config is
loaded, and again on every request by `SafeEgressTransport` (so a hostname
that passed at save time but later re-resolves to a private address — DNS
rebinding — is still refused).

Self-hosters who run a local model server (e.g. Ollama on localhost) can set
`BYO_ALLOW_PRIVATE_HOSTS=true`, which permits plain http and private hosts.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeURLError(ValueError):
    """The URL is not allowed as an outbound endpoint. The message is safe to
    show to the user."""


def allow_private_hosts() -> bool:
    return os.getenv("BYO_ALLOW_PRIVATE_HOSTS", "false").strip().lower() in ("1", "true", "yes")


def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # is_global also excludes shared address space (100.64.0.0/10, CGNAT),
    # documentation and benchmarking ranges that the flags above miss.
    return ip.is_global


def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (A and AAAA)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_host(host: str, port: int) -> None:
    """Raise UnsafeURLError unless every resolved address is public."""
    if allow_private_hosts():
        return
    try:
        addresses = resolve_host(host, port)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise UnsafeURLError(f"The host '{host}' could not be resolved.") from e
    if not addresses:
        raise UnsafeURLError(f"The host '{host}' could not be resolved.")
    blocked = [a for a in addresses if not _is_public(a)]
    if blocked:
        raise UnsafeURLError(
            f"The host '{host}' points to a private or internal network address, which isn't allowed."
        )


def validate_public_https_url(url: str | None) -> str:
    """Return the cleaned URL, or raise UnsafeURLError with a user-safe reason."""
    text = (url or "").strip()
    if not text:
        raise UnsafeURLError("The base URL is empty.")
    if len(text) > 2048:
        raise UnsafeURLError("The base URL is too long.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError("The base URL isn't a valid URL.") from e
    allowed_schemes = ("https", "http") if allow_private_hosts() else ("https",)
    if parts.scheme.lower() not in allowed_schemes:
        raise UnsafeURLError("The base URL must start with https://.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("The base URL must not contain a username or password.")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("The base URL has no host name.")
    check_host(host, port or (443 if parts.scheme.lower() == "https" else 80))
    return text


class SafeEgressTransport(httpx.HTTPTransport):
    """httpx transport that re-checks the destination host on every request,
    so a host that re-resolves to a private address after it was saved is
    refused at call time.

    Residual risk: the check resolves the name, then httpx resolves it again
    to connect; a resolver that flips between those two lookups could still
    win. Pinning the connection to the checked IP would close that and is
    tracked as future work in ARCHITECTURE.md."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        scheme = url.scheme.lower()
        if scheme != "https" and not allow_private_hosts():
            raise UnsafeURLError("Only https endpoints are allowed.")
        check_host(url.host, url.port or (443 if scheme == "https" else 80))
        return super().handle_request(request)


def safe_http_client(timeout: float = 60.0) -> httpx.Client:
    """An httpx client for calling user-supplied endpoints. Redirects are not
    followed, so an allowed host can't bounce the request to a private one."""
    return httpx.Client(transport=SafeEgressTransport(), timeout=timeout, follow_redirects=False)
