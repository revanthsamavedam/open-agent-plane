"""Caller identity.

Two modes (AUTH_MODE env):
- ``demo`` (default): the actor is taken from the ``X-Actor`` header,
  defaulting to ``admin@example.com``. For local demos only.
- ``jwt``: an OIDC/JWT Bearer token (HS256 with ``JWT_SECRET`` in this
  reference build — swap in your IdP's JWKS verification for Entra ID /
  Okta / Google in production). Missing JWT config at startup is a hard
  error — it NEVER silently falls back to demo mode.

Caller identity is threaded through every run and audit entry. Connector
credentials are never part of caller identity: adapters run service-side.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request


def auth_mode() -> str:
    return os.environ.get("AUTH_MODE", "demo").lower()


def validate_auth_config() -> None:
    if auth_mode() == "jwt" and not os.environ.get("JWT_SECRET"):
        raise RuntimeError(
            "AUTH_MODE=jwt requires JWT_SECRET to be set; refusing to start "
            "(no silent demo fallback)"
        )


def get_actor(request: Request) -> str:
    mode = auth_mode()
    if mode == "demo":
        return request.headers.get("x-actor") or "admin@example.com"
    # jwt mode
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        _audit_auth_failure(request, "missing bearer token")
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = header.split(" ", 1)[1]
    try:
        import jwt as pyjwt

        payload = pyjwt.decode(
            token, os.environ["JWT_SECRET"], algorithms=["HS256"],
            options={"require": ["sub"]},
        )
    except Exception:
        _audit_auth_failure(request, "invalid token")
        raise HTTPException(status_code=401, detail="invalid token")
    return str(payload.get("email") or payload.get("sub"))


def _audit_auth_failure(request: Request, reason: str) -> None:
    try:
        from .store import audit

        audit(request.app.state.store, "auth_failure", actor="anonymous",
              summary=f"authentication failed: {reason}", detail=reason)
    except Exception:
        pass
