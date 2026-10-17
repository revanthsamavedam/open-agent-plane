"""RBAC (v1.1): role resolution, the permission matrix, separation of
duties on approvals/promotions, and audited authorization denials."""
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def H(actor, role=None):
    headers = {"X-Actor": actor}
    if role is not None:
        headers["X-Role"] = role
    return headers


NON_ADMINS = ["owner", "approver", "auditor", "user"]


# ---- role resolution ------------------------------------------------------

def test_demo_default_actor_is_admin_backward_compat(client):
    me = client.get("/api/me").json()
    assert me == {"actor": "admin@example.com", "role": "admin",
                  "auth_mode": "demo"}
    # The default caller can still do admin things with no headers at all.
    assert client.put("/api/settings/model", json={"model": ""}).status_code == 200


def test_demo_role_defaults_and_unknown_roles(client):
    me = client.get("/api/me", headers=H("someone@example.com")).json()
    assert me["role"] == "user", "non-default actor without X-Role is user"
    me = client.get("/api/me", headers=H("someone@example.com", "owner")).json()
    assert me["role"] == "owner"
    me = client.get("/api/me", headers=H("someone@example.com", "superuser")).json()
    assert me["role"] == "user", "unknown role strings resolve to user"
    me = client.get("/api/me", headers=H("admin@example.com", "auditor")).json()
    assert me["role"] == "auditor", "an explicit X-Role beats the default"


# ---- model + connectors: admin only ----------------------------------------

@pytest.mark.parametrize("role", NON_ADMINS)
def test_model_set_and_eval_admin_only(client, role):
    headers = H(f"{role}@example.com", role)
    assert client.put("/api/settings/model", json={"model": "openai:gpt-4o"},
                      headers=headers).status_code == 403
    assert client.post("/api/settings/model/evaluate",
                       headers=headers).status_code == 403


@pytest.mark.parametrize("role", NON_ADMINS)
def test_connector_add_and_test_admin_only(client, role):
    headers = H(f"{role}@example.com", role)
    assert client.post("/api/connectors", json={
        "catalog_id": "postgres", "config": {"standin_name": "demo"},
    }, headers=headers).status_code == 403
    conn = client.post("/api/connectors", json={
        "catalog_id": "postgres", "config": {"standin_name": "demo"},
    }).json()
    assert client.post(f"/api/connectors/{conn['id']}/test",
                       headers=headers).status_code == 403
    assert client.post(f"/api/connectors/{conn['id']}/test").status_code == 200


# ---- agent creation ----------------------------------------------------------

def test_agent_create_owner_rules(client):
    alice = H("alice@example.com", "owner")
    resp = client.post("/api/agents", json={
        "name": "Alice's agent", "owner": "alice@example.com",
    }, headers=alice)
    assert resp.status_code == 201, resp.text
    # An owner may not create an agent owned by someone else.
    assert client.post("/api/agents", json={
        "name": "Not hers", "owner": "bob@example.com",
    }, headers=alice).status_code == 403
    # Admin may create for anyone; auditor/user may not create at all.
    assert client.post("/api/agents", json={
        "name": "Admin's", "owner": "bob@example.com",
    }).status_code == 201
    for role in ("auditor", "user", "approver"):
        assert client.post("/api/agents", json={
            "name": "Nope", "owner": f"{role}@example.com",
        }, headers=H(f"{role}@example.com", role)).status_code == 403


# ---- run scoping ---------------------------------------------------------------

def _agent_owned_by(client, owner):
    resp = client.post("/api/agents", json={
        "name": f"{owner}'s agent", "owner": owner,
        "allowed_skills": ["data-lookup", "draft-report"],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_run_scoping_by_agent_owner(client):
    agent = _agent_owned_by(client, "alice@example.com")
    body = {"agent_id": agent["id"], "skill": "data-lookup", "input": "q"}
    # The owner can run their own agent (owner role and user role alike).
    run = client.post("/api/runs", json=body,
                      headers=H("alice@example.com", "owner")).json()
    assert run["status"] == "completed", run
    # Another non-admin may not run it.
    assert client.post("/api/runs", json=body,
                       headers=H("bob@example.com", "user")).status_code == 403
    assert client.post("/api/runs", json=body,
                       headers=H("bob@example.com", "owner")).status_code == 403
    # Auditor and approver may not run at all.
    for role in ("auditor", "approver"):
        assert client.post("/api/runs", json=body,
                           headers=H(f"{role}@example.com", role)).status_code == 403
    # Admin may run anyone's agent.
    run = client.post("/api/runs", json=body).json()
    assert run["status"] == "completed", run


# ---- approvals: role gate + separation of duties -------------------------------

def _awaiting_run(client, headers=None):
    agent = _agent_owned_by(client, (headers or {}).get("X-Actor", "admin@example.com"))
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "draft-report", "input": "Weekly",
    }, headers=headers).json()
    assert run["status"] == "awaiting_approval", run
    return run


def test_approve_separation_of_duties(client):
    alice = H("alice@example.com", "owner")
    run = _awaiting_run(client, headers=alice)
    # The runner cannot approve their own run — even wearing the
    # approver role, and even naming themselves in the body.
    assert client.post(f"/api/runs/{run['id']}/approve", json={},
                       headers=H("alice@example.com", "approver")).status_code == 403
    assert client.post(f"/api/runs/{run['id']}/approve",
                       json={"approver": "alice@example.com"},
                       headers=H("carol@example.com", "approver")).status_code == 403
    # A plain user may not approve at all.
    assert client.post(f"/api/runs/{run['id']}/approve", json={},
                       headers=H("bob@example.com", "user")).status_code == 403
    # A different named approver can.
    out = client.post(f"/api/runs/{run['id']}/approve", json={},
                      headers=H("approver@example.com", "approver"))
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "applied"
    assert out.json()["approver"] == "approver@example.com"


def test_admin_who_ran_it_is_still_bound_by_sod(client):
    run = _awaiting_run(client)  # created by the default admin
    # Admin role does not exempt the creator from separation of duties.
    assert client.post(f"/api/runs/{run['id']}/approve",
                       json={}).status_code == 403
    assert client.post(f"/api/runs/{run['id']}/reject",
                       json={"approver": "admin@example.com"}).status_code == 403
    # A different admin-named approver body value from an approver caller works.
    out = client.post(f"/api/runs/{run['id']}/approve",
                      json={"approver": "second-admin@example.com"},
                      headers=H("approver@example.com", "approver"))
    assert out.status_code == 200, out.text
    assert out.json()["approver"] == "second-admin@example.com"


# ---- auditor: read-only ---------------------------------------------------------

def test_auditor_is_read_only(client):
    auditor = H("auditor@example.com", "auditor")
    for path in ("/api/settings", "/api/me", "/api/catalog", "/api/connectors",
                 "/api/skills", "/api/agents", "/api/runs", "/api/approvals",
                 "/api/memory", "/api/audit", "/api/audit/verify",
                 "/api/audit/export?format=json"):
        assert client.get(path, headers=auditor).status_code == 200, path
    assert client.post("/api/memory", json={
        "tier": "private", "content": "x",
    }, headers=auditor).status_code == 403
    assert client.post("/api/agents", json={
        "name": "x", "owner": "auditor@example.com",
    }, headers=auditor).status_code == 403


# ---- memory promotion: role + not-the-owner --------------------------------------

def test_memory_promote_role_and_not_owner(client):
    bob = H("bob@example.com", "user")
    item = client.post("/api/memory", json={
        "tier": "private", "content": "bob's note",
    }, headers=bob).json()
    assert item["owner"] == "bob@example.com"
    # Wrong role (user) cannot promote.
    assert client.post(f"/api/memory/{item['id']}/promote", json={},
                       headers=bob).status_code == 403
    # The item's owner cannot promote their own item, even as approver.
    assert client.post(f"/api/memory/{item['id']}/promote", json={},
                       headers=H("bob@example.com", "approver")).status_code == 403
    # A different approver can; a different admin can (second item).
    out = client.post(f"/api/memory/{item['id']}/promote", json={},
                      headers=H("approver@example.com", "approver"))
    assert out.status_code == 200, out.text
    item2 = client.post("/api/memory", json={
        "tier": "private", "content": "bob's second note",
    }, headers=bob).json()
    out = client.post(f"/api/memory/{item2['id']}/promote",
                      json={"approver": "second-admin@example.com"})
    assert out.status_code == 200, out.text


# ---- denials are audited ------------------------------------------------------------

def test_authz_denied_events_are_audited(client):
    client.put("/api/settings/model", json={"model": "x"},
               headers=H("owner@example.com", "owner"))
    client.post("/api/connectors", json={
        "catalog_id": "postgres", "config": {"standin_name": "demo"},
    }, headers=H("auditor@example.com", "auditor"))
    entries = client.get("/api/audit?event_type=authz_denied").json()
    assert len(entries) == 2, entries
    actors = {e["actor"] for e in entries}
    assert actors == {"owner@example.com", "auditor@example.com"}
    assert all("role=" in e["summary"] for e in entries)
    assert client.get("/api/audit/verify").json()["intact"] is True
    # And they show up in exports like any other event.
    exported = client.get("/api/audit/export?format=json").json()
    assert any(e["event_type"] == "authz_denied" for e in exported)


# ---- JWT role claims ------------------------------------------------------------------

SECRET = "test-secret-for-tests-only"


def _jwt_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", SECRET)
    return TestClient(create_app(Store(str(tmp_path / "rbac-jwt.db"))),
                      raise_server_exceptions=False)


def _token(email, **claims):
    return pyjwt.encode({"sub": email, "email": email, **claims},
                        SECRET, algorithm="HS256")


def test_jwt_role_claims(monkeypatch, tmp_path):
    client = _jwt_client(monkeypatch, tmp_path)
    # role claim (string): approver — can read, cannot set the model.
    approver = {"Authorization": f"Bearer {_token('appr@example.com', role='approver')}"}
    assert client.get("/api/me", headers=approver).json()["role"] == "approver"
    assert client.put("/api/settings/model", json={"model": ""},
                      headers=approver).status_code == 403
    # roles claim (list): first recognized role wins.
    admin = {"Authorization": f"Bearer {_token('boss@example.com', roles=['bogus', 'admin'])}"}
    assert client.get("/api/me", headers=admin).json()["role"] == "admin"
    assert client.put("/api/settings/model", json={"model": ""},
                      headers=admin).status_code == 200
    # role claim beats the roles list when both are present and recognized.
    both = {"Authorization": f"Bearer {_token('o@example.com', role='owner', roles=['admin'])}"}
    assert client.get("/api/me", headers=both).json()["role"] == "owner"
    # No recognized role -> user.
    plain = {"Authorization": f"Bearer {_token('plain@example.com')}"}
    assert client.get("/api/me", headers=plain).json()["role"] == "user"
    assert client.put("/api/settings/model", json={"model": ""},
                      headers=plain).status_code == 403
