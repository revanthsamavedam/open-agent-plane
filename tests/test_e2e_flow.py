"""The full demo flow over HTTP: setup wizard steps -> run -> reviewer ->
approval -> applied, with the audit trail proving every step."""
from conftest import add_agent, add_live_connector


def test_demo_flow_end_to_end(client):
    # Step 1: model (blank = TestModel placeholder) + certification eval.
    resp = client.put("/api/settings/model", json={"model": ""})
    assert resp.status_code == 200
    resp = client.post("/api/settings/model/evaluate")
    results = resp.json()["results"]
    assert results and all(r["passed"] for r in results)
    assert all(r["placeholder"] for r in results), "TestModel eval must be labelled placeholder"

    # Step 2: a LIVE connector against its stand-in.
    conn = add_live_connector(client, "postgres")
    health = client.post(f"/api/connectors/{conn['id']}/test").json()
    assert health["ok"] is True and health["live"] is True

    # Step 3: first agent.
    agent = add_agent(client, [conn["id"]])

    # Step 4a: read-only skill finishes without approval, and reads real rows.
    resp = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "data-lookup", "input": "list records",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    })
    run = resp.json()
    assert run["status"] == "completed", run
    assert "alpha" in run["draft"]
    assert run["tool_calls"] and run["tool_calls"][0]["input_hash"]

    # Step 4b: write skill -> reviewer -> awaiting approval -> approve -> applied.
    resp = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "draft-report", "input": "Weekly status",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    })
    run = resp.json()
    assert run["status"] == "awaiting_approval", run
    assert run["review"]["approved"] is True
    queue = client.get("/api/approvals").json()
    assert [r["id"] for r in queue] == [run["id"]]

    resp = client.post(f"/api/runs/{run['id']}/approve",
                       json={"approver": "ops-lead@example.com"})
    applied = resp.json()
    assert applied["status"] == "applied"
    assert applied["approver"] == "ops-lead@example.com"

    # Audit proves the whole story, and the chain verifies.
    events = [e["event_type"] for e in client.get("/api/audit").json()]
    for expected in ("model_set", "model_eval", "connector_added", "connector_tested",
                     "agent_created", "run_started", "tool_call", "reviewer_verdict",
                     "run_finished", "approval"):
        assert expected in events, f"missing audit event {expected}; got {events}"
    assert client.get("/api/audit/verify").json()["intact"] is True

    # Evidence pack for the applied run.
    pack = client.get(f"/api/runs/{run['id']}/evidence").json()
    assert pack["chain_verification"]["intact"] is True
    assert pack["approvals"] and pack["approvals"][0]["live"] is True
    assert pack["audit_slice"], "evidence pack must carry the run's audit slice"

    # Exports work.
    assert client.get("/api/audit/export?format=json").status_code == 200
    csv_resp = client.get("/api/audit/export?format=csv")
    assert csv_resp.status_code == 200 and "event_type" in csv_resp.text


def test_scaffold_connector_run_labels_recorded_intent(client):
    conn_resp = client.post("/api/connectors", json={
        "catalog_id": "slack", "config": {"api_key_ref": "env:SLACK_TOKEN"},
    })
    assert conn_resp.status_code == 201, conn_resp.text
    conn = conn_resp.json()
    agent = add_agent(client, [conn["id"]])
    resp = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "draft-report", "input": "Comms summary",
        "connector_id": conn["id"], "action": "list_channels",
    })
    run = resp.json()
    assert run["status"] == "awaiting_approval", run
    assert run["tool_calls"][0]["scaffold"] is True
    applied = client.post(f"/api/runs/{run['id']}/approve",
                          json={"approver": "ops-lead@example.com"}).json()
    assert applied["status"] == "applied"
    assert "SCAFFOLD" in applied["applied_note"]


def test_rejection_ends_run(client):
    conn = add_live_connector(client, "s3_athena")
    agent = add_agent(client, [conn["id"]])
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "daily-brief", "input": "today",
        "connector_id": conn["id"], "action": "list",
    }).json()
    assert run["status"] == "awaiting_approval", run
    out = client.post(f"/api/runs/{run['id']}/reject",
                      json={"approver": "ops-lead@example.com", "reason": "not needed"}).json()
    assert out["status"] == "rejected"
    events = [e["event_type"] for e in client.get("/api/audit").json()]
    assert "rejection" in events


def test_non_select_read_rejected(client):
    conn = add_live_connector(client, "postgres")
    agent = add_agent(client, [conn["id"]])
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "data-lookup", "input": "delete everything",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "DELETE FROM records"},
    }).json()
    assert run["status"] == "failed"
    assert "SELECT" in run["error"]


def test_disallowed_skill_fails_closed(client):
    conn = add_live_connector(client, "postgres")
    agent = add_agent(client, [conn["id"]], skills=("data-lookup",))
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "draft-report", "input": "x",
        "connector_id": conn["id"],
    }).json()
    assert run["status"] == "failed"
