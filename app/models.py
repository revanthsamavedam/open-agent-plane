"""Domain models for Open Agent Plane.

Every domain/store object is a validated Pydantic v2 model. No dataclasses.
Note (hard-won lesson): Pydantic ``PrivateAttr`` values passed through the
constructor can be silently dropped in some environments, so objects that
need a live store handle take it via an explicit ``__init__(_store=None,
**data)`` and assign it after ``super().__init__``.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


SECRET_REF_RE = re.compile(r"^(env|vault|secret):[A-Za-z0-9_./\-]+$")
RAW_SECRET_HINT_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}|-----BEGIN)"
)


def looks_like_raw_secret(value: str) -> bool:
    return bool(RAW_SECRET_HINT_RE.search(value or ""))


def redact(text: str, limit: int = 200) -> str:
    """Redacted one-line summary safe for audit logs."""
    if not text:
        return ""
    cleaned = RAW_SECRET_HINT_RE.sub("[REDACTED]", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


class ConnectorInstance(BaseModel):
    id: str = Field(default_factory=lambda: new_id("conn"))
    catalog_id: str
    display_name: str = ""
    # config values are plain strings or secret *references* (env:/vault:/secret:)
    config: dict[str, str] = Field(default_factory=dict)
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    last_health: Optional[dict[str, Any]] = None


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent"))
    name: str
    owner: str  # a named human, always
    description: str = ""
    allowed_connectors: list[str] = Field(default_factory=list)  # connector instance ids
    allowed_skills: list[str] = Field(default_factory=list)
    model_override: Optional[str] = None
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ToolCall(BaseModel):
    connector_id: str = ""
    catalog_id: str = ""
    action: str = ""
    input_hash: str = ""
    input_summary: str = ""
    result_summary: str = ""
    scaffold: bool = False
    at: datetime = Field(default_factory=utcnow)


class ReviewVerdict(BaseModel):
    approved: bool
    reviewer: str = "reviewer-agent"
    reasons: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=utcnow)


RunStatus = Literal[
    "running", "awaiting_approval", "applied", "completed",
    "rejected", "vetoed", "failed",
]


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    agent_id: str
    skill: str
    input: str = ""
    connector_id: str = ""  # connector instance id, optional
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = "running"
    draft: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    review: Optional[ReviewVerdict] = None
    model_used: str = ""
    model_placeholder: bool = False
    created_by: str = ""
    approver: str = ""
    applied_note: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None

    _store: Any = PrivateAttr(default=None)

    def __init__(self, _store: Any = None, **data: Any):
        super().__init__(**data)
        self._store = _store

    def save(self) -> None:
        if self._store is not None:
            self._store.put_run(self)


class AuditEntry(BaseModel):
    seq: int = 0
    at: datetime = Field(default_factory=utcnow)
    event_type: str
    actor: str = ""
    agent_id: str = ""
    connector_id: str = ""
    run_id: str = ""
    summary: str = ""
    # hashes of any sensitive inputs — raw values are never stored here
    detail_hashes: dict[str, str] = Field(default_factory=dict)
    prev_hash: str = "GENESIS"
    entry_hash: str = ""

    def canonical(self) -> str:
        payload = self.model_dump(mode="json", exclude={"entry_hash"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        return sha256_text(self.prev_hash + "|" + self.canonical())


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    tier: Literal["private", "team", "org"]
    owner: str = ""  # user for private, team name for team, "org" for org
    content: str
    version: int = 1
    created_by: str = ""
    promoted_from: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    # Summaries are derived views over other memory items — never
    # replacements. A summary item records exactly which items it was
    # derived from (source_ids), which approved run produced it, and
    # which model generated it (placeholder labelled via the run).
    kind: Literal["fact", "summary"] = "fact"
    source_ids: list[str] = Field(default_factory=list)
    generated_by_run_id: str = ""
    model: str = ""


class EvalResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("eval"))
    model: str
    skill: str
    passed: bool
    placeholder: bool = False  # True when run against the offline TestModel
    checks: dict[str, bool] = Field(default_factory=dict)
    detail: str = ""
    at: datetime = Field(default_factory=utcnow)


class AppliedWrite(BaseModel):
    id: str = Field(default_factory=lambda: new_id("applied"))
    run_id: str
    connector_id: str = ""
    catalog_id: str = ""
    live: bool = False  # False = intent recorded for a SCAFFOLD connector
    note: str = ""
    approved_by: str = ""
    at: datetime = Field(default_factory=utcnow)


class ModelSettings(BaseModel):
    model: str = ""  # empty = AGENTPLANE_MODEL env, else offline TestModel
    updated_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
