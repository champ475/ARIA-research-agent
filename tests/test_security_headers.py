"""SEC-15: every response carries the standard header set."""

from __future__ import annotations

import gradio as gr
import pytest
from fastapi.testclient import TestClient

import config
from web.security_headers import build_csp, header_set


@pytest.fixture
def client():
    from server import create_app

    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c


@pytest.mark.parametrize("path", ["/login", "/healthz", "/signup"])
def test_headers_on_every_response(client, path):
    r = client.get(path)
    h = {k.lower(): v for k, v in r.headers.items()}
    assert "frame-ancestors" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"
    assert "permissions-policy" in h


def test_redirect_and_missing_routes_also_covered(client):
    for r in (client.get("/", follow_redirects=False), client.get("/nope-404")):
        assert "content-security-policy" in r.headers


def test_frame_ancestors_none_adds_x_frame_options():
    names = dict(header_set(frame_ancestors="'none'", hsts=False))
    assert names[b"x-frame-options"] == b"DENY"
    assert b"frame-ancestors 'none'" in names[b"content-security-policy"]


def test_embedding_relaxes_frame_options():
    names = dict(header_set(frame_ancestors="https://huggingface.co", hsts=False))
    assert b"x-frame-options" not in names
    assert b"frame-ancestors https://huggingface.co" in names[b"content-security-policy"]


def test_hsts_only_when_enabled():
    assert b"strict-transport-security" in dict(header_set(hsts=True))
    assert b"strict-transport-security" not in dict(header_set(hsts=False))


def test_csp_blocks_plugins_and_foreign_forms():
    csp = build_csp("'none'")
    assert "object-src 'none'" in csp and "form-action 'self'" in csp and "base-uri 'self'" in csp
    assert "challenges.cloudflare.com" not in csp
    assert "challenges.cloudflare.com" in build_csp("'none'", turnstile=True)


def test_prod_defaults_enable_hsts():
    assert config.Settings(_env_file=None, APP_ENV="prod").hsts_enabled is True
    assert config.Settings(_env_file=None, APP_ENV="dev").hsts_enabled is False
