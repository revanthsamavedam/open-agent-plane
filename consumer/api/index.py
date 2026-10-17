"""PocketPlane (consumer edition of Open Agent Plane) API.

A STATELESS proxy. The visitor's own model API key arrives per-request in
the JSON body, is used exactly once to call their chosen provider, and is
then discarded. It is never persisted, never logged, and never echoed in
any response — error messages are sanitized to strip the key if a provider
reflects it back.

Routes:
- GET  /api/health    liveness probe
- POST /api/chat      one chat completion against the visitor's provider
- POST /api/test-key  tiny call that validates a key
"""
from __future__ import annotations

import os
from typing import Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_VERSION = "0.3.0"

MAX_MESSAGES = 50
MAX_CONTENT_CHARS = 8_000
MAX_SYSTEM_CHARS = 8_000
MAX_TOTAL_CHARS = 60_000
MAX_MODEL_CHARS = 200

Provider = Literal["openai", "anthropic", "openrouter", "google", "custom"]


# ---------------------------------------------------------------- models

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ChatRequest(BaseModel):
    provider: Provider
    model: str = Field(min_length=1, max_length=MAX_MODEL_CHARS)
    api_key: str = ""
    base_url: Optional[str] = None
    system: str = Field(default="", max_length=MAX_SYSTEM_CHARS)
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)


class TestKeyRequest(BaseModel):
    provider: Provider
    model: str = Field(min_length=1, max_length=MAX_MODEL_CHARS)
    api_key: str = ""
    base_url: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class ChatResponse(BaseModel):
    reply: str
    provider: str
    model: str
    usage: Optional[Usage] = None


class TestKeyResponse(BaseModel):
    ok: bool
    provider: str
    model: str


# ---------------------------------------------------------------- helpers

def _sanitize_no_proxy() -> None:
    """httpx 0.28 crashes at client construction when NO_PROXY contains a
    bracketed IPv6 entry (e.g. "[::1]"). Drop bracketed entries in place;
    a no-op where the variable is unset or clean."""
    for var in ("no_proxy", "NO_PROXY"):
        raw = os.environ.get(var)
        if raw and "[" in raw:
            kept = [p.strip() for p in raw.split(",")
                    if p.strip() and "[" not in p]
            os.environ[var] = ",".join(kept)


def _client(timeout: float) -> httpx.AsyncClient:
    """Client factory (patched with a MockTransport in tests)."""
    _sanitize_no_proxy()
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout))


def _sanitize(text: str, api_key: str) -> str:
    """Strip the key (and any URL it rides in) from text before it leaves."""
    if api_key:
        text = text.replace(api_key, "***")
    return text.strip()[:500]


def _require_key(req) -> str:
    key = (req.api_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail=(
                "No API key provided. Add your model provider token in "
                "Settings — it stays in your browser and is only used to "
                "call your provider."
            ),
        )
    return key


def _require_base_url(req) -> str:
    base = (req.base_url or "").strip().rstrip("/")
    if req.provider == "custom":
        if not base:
            raise HTTPException(
                status_code=400,
                detail="A base URL is required for the custom provider.",
            )
        if not (base.startswith("https://") or base.startswith("http://")):
            raise HTTPException(
                status_code=400,
                detail="The custom base URL must start with http:// or https://.",
            )
    return base


# ------------------------------------------------------- provider plumbing

def _openai_style_payload(model: str, system: str,
                          messages: list[Message],
                          max_tokens: Optional[int]) -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs += [{"role": m.role, "content": m.content} for m in messages]
    payload: dict = {"model": model, "messages": msgs}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload


def _build_call(provider: str, model: str, api_key: str, base_url: str,
                system: str, messages: list[Message],
                max_tokens: Optional[int]) -> tuple[str, dict, dict]:
    """Return (url, headers, json payload) for one provider call."""
    if provider in ("openai", "openrouter", "custom"):
        if provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
        elif provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
        else:
            url = (base_url if base_url.endswith("/chat/completions")
                   else f"{base_url}/chat/completions")
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["X-Title"] = "PocketPlane"
        return url, headers, _openai_style_payload(model, system, messages,
                                                   max_tokens)

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key,
                   "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {
            "model": model,
            "max_tokens": max_tokens or 2048,
            "messages": [{"role": m.role, "content": m.content}
                         for m in messages],
        }
        if system:
            payload["system"] = system
        return url, headers, payload

    # google (Gemini): key travels as a query param by Google's own design;
    # it is never written into any response or error we produce.
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{quote(model, safe='')}:generateContent?key={api_key}")
    headers = {"Content-Type": "application/json"}
    contents = [{
        "role": "model" if m.role == "assistant" else "user",
        "parts": [{"text": m.content}],
    } for m in messages]
    payload = {"contents": contents}
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    if max_tokens:
        payload["generationConfig"] = {"maxOutputTokens": max_tokens}
    return url, headers, payload


def _usage_openai_style(data: dict) -> Optional[Usage]:
    u = data.get("usage") or {}
    if not u:
        return None
    return Usage(prompt_tokens=u.get("prompt_tokens"),
                 completion_tokens=u.get("completion_tokens"),
                 total_tokens=u.get("total_tokens"))


def _parse_reply(provider: str, data: dict) -> tuple[str, Optional[Usage]]:
    if provider in ("openai", "openrouter", "custom"):
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("provider returned no choices")
        reply = (choices[0].get("message") or {}).get("content") or ""
        return reply, _usage_openai_style(data)

    if provider == "anthropic":
        parts = [b.get("text", "") for b in data.get("content") or []
                 if b.get("type") == "text"]
        u = data.get("usage") or {}
        usage = None
        if u:
            inp, out = u.get("input_tokens"), u.get("output_tokens")
            usage = Usage(
                prompt_tokens=inp,
                completion_tokens=out,
                total_tokens=(inp + out) if (inp is not None
                                              and out is not None) else None,
            )
        return "".join(parts), usage

    # google
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("provider returned no candidates")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    reply = "".join(p.get("text", "") for p in parts)
    um = data.get("usageMetadata") or {}
    usage = None
    if um:
        usage = Usage(prompt_tokens=um.get("promptTokenCount"),
                      completion_tokens=um.get("candidatesTokenCount"),
                      total_tokens=um.get("totalTokenCount"))
    return reply, usage


def _provider_error_message(resp: httpx.Response, api_key: str) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else None
        if not msg:
            msg = data.get("message") or data.get("detail")
        if msg:
            return _sanitize(str(msg), api_key)
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


async def _call_provider(req, messages: list[Message],
                         max_tokens: Optional[int],
                         timeout: float) -> tuple[str, Optional[Usage]]:
    api_key = _require_key(req)
    base_url = _require_base_url(req)
    url, headers, payload = _build_call(
        req.provider, req.model, api_key, base_url,
        getattr(req, "system", "") or "", messages, max_tokens)
    try:
        async with _client(timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception as exc:  # transport errors, proxy/env URL errors, …
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the {req.provider} API: "
                   f"{_sanitize(str(exc), api_key)}",
        )
    if resp.status_code >= 400:
        msg = _provider_error_message(resp, api_key)
        raise HTTPException(
            status_code=502,
            detail=f"{req.provider} API error ({resp.status_code}): {msg}",
        )
    try:
        return _parse_reply(req.provider, resp.json())
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"The {req.provider} API returned an unreadable response.",
        )


# ---------------------------------------------------------------- routes

app = FastAPI(title="PocketPlane", version=APP_VERSION)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "app": "pocketplane",
            "version": APP_VERSION, "stateless": True}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    total = sum(len(m.content) for m in req.messages) + len(req.system)
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Conversation too large (max {MAX_TOTAL_CHARS} "
                   "characters). Start a new thread or clear this one.",
        )
    reply, usage = await _call_provider(req, req.messages,
                                        max_tokens=None, timeout=60.0)
    return ChatResponse(reply=reply, provider=req.provider,
                        model=req.model, usage=usage)


@app.post("/api/test-key", response_model=TestKeyResponse)
async def test_key(req: TestKeyRequest) -> TestKeyResponse:
    ping = [Message(role="user", content="ping")]
    await _call_provider(req, ping, max_tokens=8, timeout=30.0)
    return TestKeyResponse(ok=True, provider=req.provider, model=req.model)
