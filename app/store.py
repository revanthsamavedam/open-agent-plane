"""SQLite-backed store (default, zero-setup). Postgres is optional via
DATABASE_URL — when it points at Postgres we still only require the URL to be
accepted and recorded; the built-in store speaks SQLite for the self-host
quickstart, and the DATABASE_URL seam is where a Postgres adapter plugs in.
All persistence goes through Pydantic models serialized to JSON.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Optional

from .models import (
    Agent, AppliedWrite, AuditEntry, ConnectorInstance, EvalResult,
    MemoryItem, ModelSettings, Run,
)

TABLES = {
    "connectors": ConnectorInstance,
    "agents": Agent,
    "runs": Run,
    "memory": MemoryItem,
    "evals": EvalResult,
    "applied": AppliedWrite,
}


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("AGENTPLANE_DB", "data/agentplane.db")
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            for table in list(TABLES) + ["audit"]:
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- generic model persistence -------------------------------------
    def _put(self, table: str, obj: Any) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (obj.id, obj.model_dump_json()),
            )
            self._conn.commit()

    def _get(self, table: str, obj_id: str):
        cls = TABLES[table]
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (obj_id,)
            ).fetchone()
        return cls.model_validate_json(row["payload"]) if row else None

    def _all(self, table: str) -> list:
        cls = TABLES[table]
        with self._lock:
            rows = self._conn.execute(f"SELECT payload FROM {table}").fetchall()
        return [cls.model_validate_json(r["payload"]) for r in rows]

    # connectors
    def put_connector(self, c: ConnectorInstance) -> None: self._put("connectors", c)
    def get_connector(self, cid: str) -> Optional[ConnectorInstance]: return self._get("connectors", cid)
    def all_connectors(self) -> list[ConnectorInstance]: return self._all("connectors")

    # agents
    def put_agent(self, a: Agent) -> None: self._put("agents", a)
    def get_agent(self, aid: str) -> Optional[Agent]: return self._get("agents", aid)
    def all_agents(self) -> list[Agent]: return self._all("agents")

    # runs (Run.save() calls back into this)
    def put_run(self, r: Run) -> None: self._put("runs", r)
    def get_run(self, rid: str) -> Optional[Run]: return self._get("runs", rid)
    def all_runs(self) -> list[Run]: return self._all("runs")

    # memory / evals / applied
    def put_memory(self, m: MemoryItem) -> None: self._put("memory", m)
    def get_memory(self, mid: str) -> Optional[MemoryItem]: return self._get("memory", mid)
    def all_memory(self) -> list[MemoryItem]: return self._all("memory")
    def put_eval(self, e: EvalResult) -> None: self._put("evals", e)
    def all_evals(self) -> list[EvalResult]: return self._all("evals")
    def put_applied(self, a: AppliedWrite) -> None: self._put("applied", a)
    def all_applied(self) -> list[AppliedWrite]: return self._all("applied")

    # settings
    def get_settings(self) -> ModelSettings:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM settings WHERE k = 'model'"
            ).fetchone()
        if row:
            return ModelSettings.model_validate_json(row["v"])
        env_model = os.environ.get("AGENTPLANE_MODEL", "")
        return ModelSettings(model=env_model)

    def put_settings(self, s: ModelSettings) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (k, v) VALUES ('model', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (s.model_dump_json(),),
            )
            self._conn.commit()

    # ---- audit: append-only, hash-chained ------------------------------
    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        """The ONLY way audit rows are written. There is deliberately no
        update or delete path for audit entries anywhere in the codebase."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM audit ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row:
                last = AuditEntry.model_validate_json(row["payload"])
                entry.seq = last.seq + 1
                entry.prev_hash = last.entry_hash
            else:
                entry.seq = 1
                entry.prev_hash = "GENESIS"
            entry.entry_hash = entry.compute_hash()
            self._conn.execute(
                "INSERT INTO audit (id, payload) VALUES (?, ?)",
                (f"audit_{entry.seq}", entry.model_dump_json()),
            )
            self._conn.commit()
        return entry

    def all_audit(self) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM audit ORDER BY rowid ASC"
            ).fetchall()
        return [AuditEntry.model_validate_json(r["payload"]) for r in rows]

    def verify_audit_chain(self) -> dict[str, Any]:
        entries = self.all_audit()
        prev = "GENESIS"
        for e in entries:
            if e.prev_hash != prev:
                return {"intact": False, "checked": e.seq, "broken_at_seq": e.seq,
                        "reason": "prev_hash mismatch"}
            if e.compute_hash() != e.entry_hash:
                return {"intact": False, "checked": e.seq, "broken_at_seq": e.seq,
                        "reason": "entry_hash mismatch (contents tampered)"}
            prev = e.entry_hash
        return {"intact": True, "checked": len(entries), "broken_at_seq": None,
                "reason": ""}


def audit(store: Store, event_type: str, actor: str = "", summary: str = "",
          agent_id: str = "", connector_id: str = "", run_id: str = "",
          detail: str = "") -> AuditEntry:
    """Append an audit entry. ``detail`` is hashed, never stored raw."""
    from .models import sha256_text
    hashes = {"detail_sha256": sha256_text(detail)} if detail else {}
    return store.append_audit(AuditEntry(
        event_type=event_type, actor=actor, summary=summary,
        agent_id=agent_id, connector_id=connector_id, run_id=run_id,
        detail_hashes=hashes,
    ))
