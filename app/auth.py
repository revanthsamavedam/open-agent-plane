"""Caller identity + RBAC role resolution.

Two modes (AUTH_MODE env):
- ``demo`` (default): the actor is taken from the ``X-Actor`` header,
  defaulting to ``admin@example.com``, and the role from the ``X-Role``
  header. For local demos only.
- ``jwt``: an OIDC/JWT Bearer token (HS256 with ``JWT_SECRET`` in this
  reference build — swap in your IdP's JWKS verification for Entra ID /
  Okta / Google in production). The role comes from the token's ``role``
  claim (string) or ``roles`` claim (list); the first recognized role
  wins. Missing JWT config at startup is a hard error — it NEVER
  silently falls back to demo mode.

Roles: ``admin``, ``owner``, ``approver``, ``auditor``, ``user``.
Unrecognized or absent roles resolve to ``user`` — except in demo mode,
where the default actor (``admin@example.com``) with no ``X-Role``
header defaults to ``admin`` (backward compatibility for local demos).

Caller identity (actor + role) is threaded through every run and audit
entry. Authorization itself is enforced server-side per endpoint (see
app/main.py); every denial is audited as ``authz_denied``.
Connector credentials are never part of caller identity: adapters run
service-side.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from pydantic import BaseModel

ROLES = ("admin", "owner", "approver", "auditor", "user")
DEFAULT_ACTOR = "admin@example.com"


class Caller(BaseModel):
    """The authenticated caller: who they are and the role they act in."""

    actor: str
    role: str = "user"


def auth_mode() -> str:
    return os.environ.get("AUTH_MODE", "demo").lower()


def validate_auth_config() -> None:
    if auth_mode() == "jwt" and not os.environ.get("JWT_SECRET"):
        raise RuntimeError(
            "AUTH_MODE=jwt requires JWT_SECRET to be set; refusing to start "
            "(no silent demo fallback)"
        )


def resolve_role(raw) -> str:
    """Map an arbitrary claim/header value to a known role.

    A list yields its first recognized entry; anything unrecognized
    resolves to ``user``.
    """
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip().lower() in ROLES:
                return item.strip().lower()
        return "user"
    if isinstance(raw, str) and raw.strip().lower() in ROLES:
        return raw.strip().lower()
    return "user"


def _role_from_claims(payload: dict) -> str:
    """JWT role claims: ``role`` (string) first, then ``roles`` (list)."""
    if "role" in payload:
        role = resolve_role(payload.get("role"))
        if role != "user" or str(payload.get("role")).strip().lower() == "user":
            return role
    if "roles" in payload:
        return resolve_role(payload.get("roles"))
    return "user"


def _decode_jwt(request: Request) -> dict:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        _audit_auth_failure(request, "missing bearer token")
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = header.split(" ", 1)[1]
    try:
        import jwt as pyjwt

        return pyjwt.decode(
            token, os.environ["JWT_SECRET"], algorithms=["HS256"],
            options={"require": ["sub"]},
        )
    except Exception:
        _audit_auth_failure(request, "invalid token")
        raise HTTPException(status_code=401, detail="invalid token")


def get_caller(request: Request) -> Caller:
    """FastAPI dependency: resolve the caller (actor + role)."""
    if auth_mode() == "demo":
        actor = request.headers.get("x-actor") or DEFAULT_ACTOR
        raw_role = request.headers.get("x-role")
        if raw_role is not None:
            role = resolve_role(raw_role)
        elif actor == DEFAULT_ACTOR:
            role = "admin"
        else:
            role = "user"
        return Caller(actor=actor, role=role)
    payload = _decode_jwt(request)
    actor = str(payload.get("email") or payload.get("sub"))
    return Caller(actor=actor, role=_role_from_claims(payload))


def get_actor(request: Request) -> str:
    return get_caller(request).actor


def get_role(request: Request) -> str:
    return get_caller(request).role


def _audit_auth_failure(request: Request, reason: str) -> None:
    try:
        from .store import audit

        audit(request.app.state.store, "auth_failure", actor="anonymous",
              summary=f"authentication failed: {reason}", detail=reason)
    except Exception:
        pass
