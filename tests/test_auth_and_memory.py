import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def test_jwt_mode_requires_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="no silent demo fallback"):
        create_app(Store(str(tmp_path / "x.db")))


def test_jwt_mode_accepts_valid_token_and_rejects_bad(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-tests-only")
    client = TestClient(create_app(Store(str(tmp_path / "y.db"))), raise_server_exceptions=False)
    assert client.get("/api/agents").status_code == 401
    token = pyjwt.encode({"sub": "user-1", "email": "person@example.com"},
                         "test-secret-for-tests-only", algorithm="HS256")
    resp = client.get("/api/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    failures = [e for e in client.get(
        "/api/audit", headers={"Authorization": f"Bearer {token}"}).json()
        if e["event_type"] == "auth_failure"]
    assert failures, "auth failures must be audited"


def test_memory_tiers_and_promotion_is_audited(client):
    item = client.post("/api/memory", json={
        "tier": "private", "owner": "person@example.com", "content": "prefers terse briefs",
    }).json()
    assert item["tier"] == "private"
    promoted = client.post(f"/api/memory/{item['id']}/promote",
                           json={"approver": "team-lead@example.com"})
    assert promoted.status_code == 200
    assert promoted.json()["tier"] == "team"
    team = client.get("/api/memory?tier=team").json()
    assert len(team) == 1
    events = [e["event_type"] for e in client.get("/api/audit").json()]
    assert "memory_added" in events and "memory_promoted" in events


def test_model_gate_blocks_uncertified_real_model(client, store):
    # A "real" model string with no passing eval cannot run production skills.
    client.put("/api/settings/model", json={"model": "openai:gpt-4o"})
    agent_resp = client.post("/api/agents", json={
        "name": "A", "owner": "o@example.com", "allowed_skills": ["data-lookup"],
    })
    agent = agent_resp.json()
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "data-lookup", "input": "q",
    }).json()
    assert run["status"] == "failed"
    assert "certification" in run["error"]
