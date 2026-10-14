"""SEC-01 — a BYO base URL may only reach public https endpoints."""

import socket
from unittest.mock import patch

import httpx
import pytest

from agents.llm_client import LLMProvidersExhausted, format_exhausted_message
from utils import url_safety
from utils.url_safety import SafeEgressTransport, UnsafeURLError, validate_public_https_url

_PUBLIC_IP = "104.18.6.192"


def _fake_dns(mapping):
    """getaddrinfo stand-in: hostname -> list of IPs; literal IPs resolve to themselves."""

    def getaddrinfo(host, port, *args, **kwargs):
        ips = mapping.get(host, [host])
        out = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, 6, "", (ip, port)))
        return out

    return getaddrinfo


_DNS = {
    "api.openai.com": [_PUBLIC_IP],
    "openrouter.ai": ["104.18.2.115"],
    "api.groq.com": ["104.18.40.1"],
    "evil.example": ["192.168.1.5"],
    "mixed.example": [_PUBLIC_IP, "10.0.0.7"],
    "v6local.example": ["fd00::1"],
}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.delenv("BYO_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", _fake_dns(_DNS))


@pytest.mark.parametrize(
    "url",
    [
        "http://openai.com",
        "https://127.0.0.1",
        "https://169.254.169.254/latest/meta-data",
        "https://10.1.2.3",
        "https://[::1]",
        "https://evil.example/v1",
        "https://mixed.example/v1",
        "https://v6local.example/v1",
        "https://100.64.0.1",
        "https://[::ffff:127.0.0.1]",
        "https://0.0.0.0",
        "https://user:pw@api.openai.com/v1",
        "ftp://api.openai.com",
        "file:///etc/passwd",
        "https://",
        "",
        "   ",
        "not a url",
        "https://api.openai.com:99999",
        "https://" + "a" * 3000 + ".com",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_public_https_url(url)


@pytest.mark.parametrize(
    "url", ["https://api.openai.com/v1", "https://openrouter.ai/api/v1", "https://api.groq.com/openai/v1"]
)
def test_accepts_public_https_providers(url):
    assert validate_public_https_url(f"  {url} ") == url


def test_unresolvable_host_is_rejected(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fail)
    with pytest.raises(UnsafeURLError, match="could not be resolved"):
        validate_public_https_url("https://does-not-exist.example")


def test_operator_flag_allows_local_model_servers(monkeypatch):
    monkeypatch.setenv("BYO_ALLOW_PRIVATE_HOSTS", "true")
    assert validate_public_https_url("http://localhost:11434/v1") == "http://localhost:11434/v1"


def test_error_messages_never_include_resolved_addresses():
    with pytest.raises(UnsafeURLError) as exc:
        validate_public_https_url("https://evil.example/v1")
    assert "192.168" not in str(exc.value)


def test_transport_blocks_host_that_rebinds_to_private_after_validation(monkeypatch):
    calls = {"n": 0}

    def rebinding(host, port, *a, **k):
        calls["n"] += 1
        ip = _PUBLIC_IP if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", rebinding)
    assert validate_public_https_url("https://rebind.example/v1")  # first lookup: public
    sent = []
    with (
        patch.object(httpx.HTTPTransport, "handle_request", side_effect=lambda req: sent.append(req)),
        pytest.raises(UnsafeURLError),
    ):
        SafeEgressTransport().handle_request(httpx.Request("POST", "https://rebind.example/v1/chat/completions"))
    assert sent == []  # nothing left the process


def test_transport_passes_public_hosts_through():
    ok = httpx.Response(200)
    with patch.object(httpx.HTTPTransport, "handle_request", return_value=ok) as inner:
        resp = SafeEgressTransport().handle_request(httpx.Request("POST", "https://api.openai.com/v1/x"))
    assert resp is ok and inner.called


def test_safe_client_does_not_follow_redirects():
    assert url_safety.safe_http_client().follow_redirects is False


def test_provider_refuses_private_base_url():
    from agents.providers.openai_compatible import OpenAICompatibleProvider

    with pytest.raises(UnsafeURLError):
        OpenAICompatibleProvider("s", "sk-test", "gpt-4o-mini", base_url="https://169.254.169.254/v1")


def test_provider_uses_the_safe_transport():
    from agents.providers.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider("s", "sk-test", "gpt-4o-mini", base_url="https://api.openai.com/v1")
    assert isinstance(p._client._client._transport, SafeEgressTransport)


@pytest.mark.parametrize("is_byo", [True, False])
def test_exhausted_message_never_echoes_raw_upstream_error(is_byo):
    secret_body = "upstream said: internal-host-banner v1.2 at 10.0.0.5 key=sk-abc123"
    msg = format_exhausted_message(LLMProvidersExhausted(RuntimeError(secret_body), is_byo=is_byo))
    assert "internal-host-banner" not in msg
    assert "10.0.0.5" not in msg
    assert "Last error" not in msg


def test_exhausted_message_names_the_error_class_for_byo():
    msg = format_exhausted_message(LLMProvidersExhausted(RuntimeError("Error code: 401 - invalid api key"), is_byo=True))
    assert "authentication failed" in msg
