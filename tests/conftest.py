import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPLANE_STANDIN_DIR", str(tmp_path / "standin"))
    import app.connectors as connectors_mod
    connectors_mod.STANDIN_DIR = tmp_path / "standin"
    return Store(str(tmp_path / "test.db"))


@pytest.fixture()
def client(store):
    from fastapi.testclient import TestClient

    return TestClient(create_app(store))


def add_live_connector(client, catalog_id="postgres", config=None):
    resp = client.post("/api/connectors", json={
        "catalog_id": catalog_id, "config": config or {"standin_name": "demo"},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_agent(client, connector_ids, skills=("daily-brief", "data-lookup", "draft-report")):
    resp = client.post("/api/agents", json={
        "name": "Ops Assistant", "owner": "ops-lead@example.com",
        "allowed_connectors": connector_ids, "allowed_skills": list(skills),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()
