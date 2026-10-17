"""Consumer-edition API tests.

Provider HTTP is mocked with httpx.MockTransport, so no real keys or
network are involved. The fake key is an obvious fixture string; several
tests assert it appears in NO response body (the stateless proxy must
never echo the visitor's key).
"""
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.index as api  # noqa: E402

FAKE_KEY = "sk-test-fixture-0000"


@pytest.fixture()
def client():
    return TestClient(api.app)


def install_transport(monkeypatch, handler):
    """Route the API's outbound httpx calls through a MockTransport."""
    def fake_client(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 timeout=httpx.Timeout(timeout))
    monkeypatch.setattr(api, "_client", fake_client)


def ok_handler(request: httpx.Request) -> httpx.Response:
    """Canned per-provider success responses."""
    host = request.url.host
    if host == "api.anthropic.com":
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "Hello from Claude"}],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        })
    if host == "generativelanguage.googleapis.com":
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "Hello from Gemini"}]}}],
            "usageMetadata": {"promptTokenCount": 9,
                               "candidatesTokenCount": 5,
                               "totalTokenCount": 14},
        })
    # openai / openrouter / custom — OpenAI-compatible shape
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant",
                                  "content": "Hello from the model"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 6,
                  "total_tokens": 16},
    })


def chat_body(**overrides):
    body = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": FAKE_KEY,
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hi there"}],
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ tests

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["stateless"] is True


def test_chat_without_key_is_clean_400(client):
    r = client.post("/api/chat", json=chat_body(api_key=""))
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]


def test_chat_openai_mocked(client, monkeypatch):
    install_transport(monkeypatch, ok_handler)
    r = client.post("/api/chat", json=chat_body())
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] == "Hello from the model"
    assert data["provider"] == "openai"
    assert data["model"] == "gpt-4o-mini"
    assert data["usage"] == {"prompt_tokens": 10, "completion_tokens": 6,
                             "total_tokens": 16}
    assert FAKE_KEY not in r.text


def test_chat_anthropic_mocked(client, monkeypatch):
    seen = {}

    def handler(request):
        seen["x-api-key"] = request.headers.get("x-api-key")
        return ok_handler(request)

    install_transport(monkeypatch, handler)
    r = client.post("/api/chat", json=chat_body(
        provider="anthropic", model="claude-sonnet-4-5"))
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] == "Hello from Claude"
    assert data["usage"]["prompt_tokens"] == 11
    assert data["usage"]["completion_tokens"] == 7
    assert data["usage"]["total_tokens"] == 18
    assert seen["x-api-key"] == FAKE_KEY  # key reached the provider…
    assert FAKE_KEY not in r.text          # …but never comes back to us


def test_chat_google_mocked(client, monkeypatch):
    install_transport(monkeypatch, ok_handler)
    r = client.post("/api/chat", json=chat_body(
        provider="google", model="gemini-2.5-flash"))
    assert r.status_code == 200
    data = r.json()
    assert data["reply"] == "Hello from Gemini"
    assert data["usage"]["total_tokens"] == 14
    assert FAKE_KEY not in r.text


def test_chat_openrouter_mocked(client, monkeypatch):
    install_transport(monkeypatch, ok_handler)
    r = client.post("/api/chat", json=chat_body(
        provider="openrouter", model="openai/gpt-4o-mini"))
    assert r.status_code == 200
    assert r.json()["reply"] == "Hello from the model"
    assert FAKE_KEY not in r.text


def test_chat_custom_base_url_mocked(client, monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return ok_handler(request)

    install_transport(monkeypatch, handler)
    r = client.post("/api/chat", json=chat_body(
        provider="custom", base_url="https://custom.example.com/v1",
        model="my-local-model"))
    assert r.status_code == 200
    assert r.json()["reply"] == "Hello from the model"
    assert seen["url"] == "https://custom.example.com/v1/chat/completions"
    assert FAKE_KEY not in r.text


def test_chat_custom_without_base_url_400(client):
    r = client.post("/api/chat", json=chat_body(provider="custom"))
    assert r.status_code == 400
    assert "base URL" in r.json()["detail"]


def test_invalid_provider_422(client):
    r = client.post("/api/chat", json=chat_body(provider="carrier-pigeon"))
    assert r.status_code == 422


def test_too_many_messages_422(client):
    msgs = [{"role": "user", "content": "x"}] * 51
    r = client.post("/api/chat", json=chat_body(messages=msgs))
    assert r.status_code == 422


def test_provider_auth_error_is_sanitized(client, monkeypatch):
    """Provider reflects the key in its error → we must strip it."""

    def handler(request):
        return httpx.Response(401, json={
            "error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}})

    install_transport(monkeypatch, handler)
    r = client.post("/api/chat", json=chat_body())
    assert r.status_code == 502
    assert FAKE_KEY not in r.text
    assert "Incorrect API key" in r.json()["detail"]


def test_test_key_ok_mocked(client, monkeypatch):
    install_transport(monkeypatch, ok_handler)
    r = client.post("/api/test-key", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": FAKE_KEY})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "provider": "openai",
                        "model": "gpt-4o-mini"}
    assert FAKE_KEY not in r.text


def test_test_key_without_key_400(client):
    r = client.post("/api/test-key", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": ""})
    assert r.status_code == 400


def test_transport_failure_is_502_not_500(client, monkeypatch):
    """If the HTTP client itself blows up (proxy/env errors), the caller
    still gets a clean 502 — never a 500, never the key."""
    def broken_client(timeout):
        raise httpx.InvalidURL("Invalid port: ':1]'")

    monkeypatch.setattr(api, "_client", broken_client)
    r = client.post("/api/chat", json=chat_body())
    assert r.status_code == 502
    assert "Could not reach" in r.json()["detail"]
    assert FAKE_KEY not in r.text


def test_no_proxy_sanitizer_strips_bracketed_ipv6(monkeypatch):
    monkeypatch.setenv("no_proxy", "localhost,[::1],example.com")
    api._sanitize_no_proxy()
    import os
    assert os.environ["no_proxy"] == "localhost,example.com"


def test_test_key_provider_failure(client, monkeypatch):
    def handler(request):
        return httpx.Response(403, json={
            "error": {"message": "Key not allowed for this model"}})

    install_transport(monkeypatch, handler)
    r = client.post("/api/test-key", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": FAKE_KEY})
    assert r.status_code == 502
    assert "Key not allowed" in r.json()["detail"]
    assert FAKE_KEY not in r.text
