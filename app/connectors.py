"""Connector catalog + adapters.

Adding a connector = adding a YAML file in ``connectors/`` (+ reusing one of
the adapter kinds). No core code changes.

Credential rule: config fields declared ``secret_ref`` must contain a
*reference* (``env:VAR``, ``vault:path``, ``secret:name``) — never a raw
secret. Adapters resolve references service-side; agents and audit logs only
ever see results and hashes.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .models import SECRET_REF_RE, looks_like_raw_secret

CATALOG_DIR = Path(__file__).resolve().parent.parent / "connectors"
STANDIN_DIR = Path(os.environ.get("AGENTPLANE_STANDIN_DIR", "data/standin"))


class ConfigField(BaseModel):
    name: str
    kind: str = "plain"  # plain | secret_ref
    required: bool = False
    description: str = ""
    example: str = ""


class CatalogEntry(BaseModel):
    id: str
    name: str
    category: str = ""
    status: str  # LIVE | SCAFFOLD
    auth_type: str  # oauth2 | api_key | service_account | basic | connection_string
    adapter: str  # sqlite | local_fs | generic_rest | scaffold
    base_url: str = ""
    scopes: list[str] = Field(default_factory=list)
    config_fields: list[ConfigField] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    notes: str = ""


def load_catalog() -> dict[str, CatalogEntry]:
    catalog: dict[str, CatalogEntry] = {}
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        entry = CatalogEntry.model_validate(data)
        catalog[entry.id] = entry
    return catalog


def resolve_ref(ref: str) -> Optional[str]:
    """Resolve a secret reference service-side. Returns None for vault/secret
    refs in the demo build (a real deployment wires its vault client here)."""
    if ref.startswith("env:"):
        return os.environ.get(ref[4:])
    return None


def validate_config(entry: CatalogEntry, config: dict[str, str]) -> list[str]:
    errors: list[str] = []
    known = {f.name: f for f in entry.config_fields}
    for field in entry.config_fields:
        value = config.get(field.name, "")
        if field.required and not value:
            errors.append(f"missing required field: {field.name}")
        if not value:
            continue
        if field.kind == "secret_ref":
            if looks_like_raw_secret(value):
                errors.append(f"{field.name}: looks like a raw secret — store a reference instead")
            elif not SECRET_REF_RE.match(value):
                errors.append(f"{field.name}: must be a reference like env:VAR or vault:path")
        elif looks_like_raw_secret(value):
            errors.append(f"{field.name}: looks like a raw secret — use a secret_ref field")
    for key in config:
        if key not in known:
            errors.append(f"unknown config field: {key}")
    return errors


class ConnectorAdapter:
    """Common connector protocol. Implementations return results only —
    never credentials."""

    kind = "base"
    live = False

    def __init__(self, entry: CatalogEntry):
        self.entry = entry

    def health(self, config: dict[str, str]) -> dict[str, Any]:
        raise NotImplementedError

    def read(self, config: dict[str, str], action: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def write_draft(self, config: dict[str, str], content: str) -> dict[str, Any]:
        raise NotImplementedError


class SqliteAdapter(ConnectorAdapter):
    """LIVE: performs real reads/writes against a local SQLite stand-in that
    stands in for the enterprise database (Postgres/MySQL shape). Only
    read-only SELECT statements are accepted for reads."""

    kind = "sqlite"
    live = True

    def _db_path(self, config: dict[str, str]) -> Path:
        name = config.get("standin_name") or self.entry.id
        path = STANDIN_DIR / f"{name}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _connect(self, config: dict[str, str]) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(config))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS records "
            "(id INTEGER PRIMARY KEY, title TEXT, detail TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drafts "
            "(id INTEGER PRIMARY KEY, content TEXT, created_at TEXT)"
        )
        if conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO records (title, detail) VALUES (?, ?)",
                [("alpha", "first sample record"), ("beta", "second sample record"),
                 ("gamma", "third sample record")],
            )
        conn.commit()
        return conn

    def health(self, config: dict[str, str]) -> dict[str, Any]:
        conn = self._connect(config)
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        conn.close()
        return {"ok": True, "live": True, "adapter": self.kind,
                "detail": f"stand-in database reachable, {n} sample records",
                "target": "local SQLite stand-in"}

    def read(self, config: dict[str, str], action: str, params: dict[str, Any]) -> dict[str, Any]:
        query = (params or {}).get("query") or "SELECT id, title, detail FROM records ORDER BY id"
        if not query.strip().lower().startswith("select"):
            raise ValueError("read-only: only SELECT statements are allowed")
        if ";" in query.strip().rstrip(";"):
            raise ValueError("read-only: single statement only")
        conn = self._connect(config)
        rows = [dict(zip([d[0] for d in cur.description], r))
                for cur in [conn.execute(query)] for r in cur.fetchall()]
        conn.close()
        return {"live": True, "rows": rows, "row_count": len(rows)}

    def write_draft(self, config: dict[str, str], content: str) -> dict[str, Any]:
        conn = self._connect(config)
        cur = conn.execute(
            "INSERT INTO drafts (content, created_at) VALUES (?, datetime('now'))",
            (content,),
        )
        conn.commit()
        draft_id = cur.lastrowid
        conn.close()
        return {"live": True, "draft_id": draft_id,
                "detail": "draft row inserted into stand-in database"}


class LocalFsAdapter(ConnectorAdapter):
    """LIVE: reads/writes a local directory stand-in for object storage
    (S3 shape). Supports list, read, and a simple CSV filter query."""

    kind = "local_fs"
    live = True

    def _root(self, config: dict[str, str]) -> Path:
        name = config.get("standin_name") or self.entry.id
        root = STANDIN_DIR / name
        root.mkdir(parents=True, exist_ok=True)
        sample = root / "sample.csv"
        if not sample.exists():
            sample.write_text("id,title,detail\n1,alpha,first\n2,beta,second\n")
        return root

    def health(self, config: dict[str, str]) -> dict[str, Any]:
        root = self._root(config)
        files = [p.name for p in root.iterdir() if p.is_file()]
        return {"ok": True, "live": True, "adapter": self.kind,
                "detail": f"stand-in bucket reachable, {len(files)} objects",
                "target": "local filesystem stand-in"}

    def read(self, config: dict[str, str], action: str, params: dict[str, Any]) -> dict[str, Any]:
        root = self._root(config)
        if action in ("list", ""):
            files = sorted(p.name for p in root.iterdir() if p.is_file())
            return {"live": True, "objects": files, "row_count": len(files)}
        name = (params or {}).get("name", "sample.csv")
        target = root / Path(name).name
        if not target.exists():
            raise ValueError(f"object not found in stand-in bucket: {name}")
        text = target.read_text()
        if target.suffix == ".csv":
            rows = list(csv.DictReader(io.StringIO(text)))
            return {"live": True, "rows": rows, "row_count": len(rows)}
        return {"live": True, "content": text[:2000], "row_count": 1}

    def write_draft(self, config: dict[str, str], content: str) -> dict[str, Any]:
        root = self._root(config)
        path = root / "draft.txt"
        path.write_text(content)
        return {"live": True, "draft_id": path.name,
                "detail": "draft object written to stand-in bucket"}


class GenericRestAdapter(ConnectorAdapter):
    """SCAFFOLD for vendor SaaS APIs. Validates config, resolves secret
    references service-side, and can probe a configured base URL health
    path — but it does NOT implement vendor-specific API semantics, so it
    is labelled SCAFFOLD everywhere. Reads/writes return a recorded
    intent rather than calling the vendor."""

    kind = "generic_rest"
    live = False

    def health(self, config: dict[str, str]) -> dict[str, Any]:
        base = config.get("base_url") or self.entry.base_url
        probed = False
        reachable: Optional[bool] = None
        if base:
            try:
                import httpx
                token = None
                for f in self.entry.config_fields:
                    if f.kind == "secret_ref":
                        token = resolve_ref(config.get(f.name, ""))
                        break
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                with httpx.Client(timeout=5.0, trust_env=False) as client:
                    resp = client.get(base, headers=headers)
                probed = True
                reachable = resp.status_code < 500
            except Exception:
                probed = True
                reachable = False
        return {"ok": True, "live": False, "adapter": self.kind,
                "scaffold": True, "base_url_configured": bool(base),
                "probed": probed, "reachable": reachable,
                "detail": "scaffold adapter: config validated; vendor API not implemented"}

    def read(self, config: dict[str, str], action: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"live": False, "scaffold": True, "recorded": True,
                "detail": f"recorded read intent '{action}' for {self.entry.id}; "
                          "vendor API call not implemented in scaffold adapter",
                "row_count": 0, "rows": []}

    def write_draft(self, config: dict[str, str], content: str) -> dict[str, Any]:
        return {"live": False, "scaffold": True, "recorded": True,
                "detail": f"recorded write intent for {self.entry.id}; "
                          "no vendor write was performed"}


class ScaffoldAdapter(GenericRestAdapter):
    kind = "scaffold"


ADAPTERS = {
    "sqlite": SqliteAdapter,
    "local_fs": LocalFsAdapter,
    "generic_rest": GenericRestAdapter,
    "scaffold": ScaffoldAdapter,
}


def get_adapter(entry: CatalogEntry) -> ConnectorAdapter:
    cls = ADAPTERS.get(entry.adapter, ScaffoldAdapter)
    return cls(entry)


def summarize_result(result: dict[str, Any]) -> str:
    safe = {k: v for k, v in result.items() if k not in ("rows", "content")}
    text = json.dumps(safe, default=str)
    if "row_count" in result:
        text += f" row_count={result['row_count']}"
    return text[:200]
