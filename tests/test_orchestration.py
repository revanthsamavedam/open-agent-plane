"""Subagent orchestration (v1.3): a run can be a PLAN executed by
governed child runs — Muse-style delegation inside the governance
plane. Children are first-class runs (eval gate, reviewer, approvals);
a child write parks the parent; the orchestration's creator can never
approve its children (separation of duties is inherited)."""
import re

from conftest import add_agent, add_live_connector

ADMIN = {}
APPROVER = {"X-Actor": "approver@example.com", "X-Role": "approver"}


def H(actor, role=None):
    headers = {"X-Actor": actor}
    if role is not None:
        headers["X-Role"] = role
    return headers


def _orchestrate(client, lead_id, task="Weekly ops task", subtasks=None,
                 headers=None):
    body = {"lead_agent_id": lead_id, "task": task}
    if subtasks is not None:
        body["subtasks"] = subtasks
    return client.post("/api/orchestrate", json=body, headers=headers or {})


def _children(client, parent_id):
    return [r for r in client.get("/api/runs").json()
            if r["parent_run_id"] == parent_id]


def _child(client, parent_id, key):
    matches = [r for r in _children(client, parent_id)
               if r["plan_step"] == key]
    assert len(matches) == 1, (parent_id, key, matches)
    return matches[0]


def _events(client, event_type=None):
    path = "/api/audit" + (f"?event_type={event_type}" if event_type else "")
    return client.get(path).json()


# ---- cataloguing -------------------------------------------------------------

def test_orchestrate_skill_catalogued_and_eval_covers_it(client):
    names = [s["name"] for s in client.get("/api/skills").json()]
    assert "orchestrate" in names
    client.put("/api/settings/model", json={"model": ""})
    results = client.post("/api/settings/model/evaluate").json()["results"]
    by_skill = {r["skill"]: r for r in results}
    assert "orchestrate" in by_skill
    assert by_skill["orchestrate"]["placeholder"] is True


def test_generic_run_with_orchestrate_fails_closed(client):
    agent = add_agent(client, [], skills=("orchestrate", "data-lookup"))
    run = client.post("/api/runs", json={
        "agent_id": agent["id"], "skill": "orchestrate",
        "input": "do it all",
    }).json()
    assert run["status"] == "failed", run
    assert "POST /api/orchestrate" in run["error"], run["error"]


# ---- explicit DAG: order + typed handoff -------------------------------------

def test_explicit_dag_order_and_template_handoff(client):
    agent = add_agent(client, [], skills=("data-lookup", "draft-report"))
    # Listed out of dependency order on purpose: c <- b <- a.
    subtasks = [
        {"key": "c", "skill": "data-lookup",
         "input": "Final check on: {{b.result}}", "depends_on": ["b"]},
        {"key": "a", "skill": "data-lookup",
         "input": "UNIQUE_FACT_XYZ lookup"},
        {"key": "b", "skill": "data-lookup",
         "input": "Extend this: {{a.result}}", "depends_on": ["a"]},
    ]
    resp = _orchestrate(client, agent["id"], subtasks=subtasks)
    assert resp.status_code == 201, resp.text
    parent = resp.json()
    assert parent["skill"] == "orchestrate"
    assert parent["status"] == "completed", parent
    assert parent["parent_run_id"] == "" and parent["plan_step"] == ""

    children = _children(client, parent["id"])
    assert len(children) == 3
    for child in children:
        assert child["status"] == "completed", child
        assert child["created_by"] == "admin@example.com"

    # Template handoff: step b's stored input carries step a's result
    # text (which embeds a's own input via the no-connector read path).
    b = _child(client, parent["id"], "b")
    assert "UNIQUE_FACT_XYZ" in b["input"], b["input"]
    assert "{{a.result}}" not in b["input"]
    c = _child(client, parent["id"], "c")
    assert "UNIQUE_FACT_XYZ" in c["input"], c["input"]

    # Execution followed dependency order (audit subtask_started order).
    started = _events(client, "subtask_started")
    keys = [re.search(r"step=(\w+)", e["summary"]).group(1)
            for e in started]
    assert keys == ["a", "b", "c"], keys
    types = [e["event_type"] for e in _events(client)]
    assert "orchestration_started" in types
    assert "orchestration_finished" in types
    assert client.get("/api/audit/verify").json()["intact"] is True

    # Parent synthesis mentions the child results; evidence pack has kids.
    assert "[a]" in parent["draft"] and "UNIQUE_FACT_XYZ" in parent["draft"]
    pack = client.get(f"/api/runs/{parent['id']}/evidence").json()
    assert len(pack["child_runs"]) == 3
    assert pack["chain_verification"]["intact"] is True


def test_unresolvable_template_fails_child_and_parent(client):
    agent = add_agent(client, [], skills=("data-lookup",))
    subtasks = [
        {"key": "a", "skill": "data-lookup", "input": "plain"},
        {"key": "b", "skill": "data-lookup",
         "input": "Use {{zzz.result}} and {{a.result}}",
         "depends_on": ["a"]},
    ]
    parent = _orchestrate(client, agent["id"], subtasks=subtasks).json()
    assert parent["status"] == "failed", parent
    assert "subtask 'b' failed" in parent["error"]
    b = _child(client, parent["id"], "b")
    assert b["status"] == "failed"
    assert "unresolvable template" in b["error"]


# ---- planner (TestModel placeholder) ------------------------------------------

def test_auto_plan_placeholder_completes_after_governed_approval(client):
    agent = add_agent(client, [],
                      skills=("data-lookup", "draft-report"))
    resp = _orchestrate(client, agent["id"], task="Weekly status rollup")
    assert resp.status_code == 201, resp.text
    parent = resp.json()
    assert parent["model_placeholder"] is True, parent
    assert parent["params"]["plan_source"] == "placeholder"
    plan = parent["params"]["plan"]
    assert [s["key"] for s in plan] == ["lookup", "report"]
    assert plan[1]["depends_on"] == ["lookup"]
    assert "{{lookup.result}}" in plan[1]["input"]

    lookup = _child(client, parent["id"], "lookup")
    assert lookup["status"] == "completed"
    # The report child is a write: the parent parks awaiting approval,
    # naming the waiting child.
    assert parent["status"] == "awaiting_approval", parent
    report = _child(client, parent["id"], "report")
    assert report["status"] == "awaiting_approval"
    assert report["id"] in parent["draft"]

    out = client.post(f"/api/runs/{report['id']}/approve", json={},
                      headers=APPROVER)
    assert out.status_code == 200, out.text
    parent = client.get(f"/api/runs/{parent['id']}").json()
    assert parent["status"] == "completed", parent
    assert "[report]" in parent["draft"]


# ---- approval interplay --------------------------------------------------------

def _write_child_orchestration(client):
    conn = add_live_connector(client)
    agent = add_agent(client, [conn["id"]],
                      skills=("data-lookup", "draft-report"))
    subtasks = [
        {"key": "gather", "skill": "data-lookup",
         "input": "list the records", "connector_id": conn["id"],
         "action": "query",
         "params": {"query": "SELECT title FROM records ORDER BY id"}},
        {"key": "report", "skill": "draft-report",
         "input": "Weekly report from: {{gather.result}}",
         "depends_on": ["gather"], "connector_id": conn["id"],
         "action": "query",
         "params": {"query": "SELECT title FROM records ORDER BY id"}},
        {"key": "wrap", "skill": "data-lookup",
         "input": "Confirm the filed report: {{report.result}}",
         "depends_on": ["report"]},
    ]
    resp = _orchestrate(client, agent["id"], subtasks=subtasks)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_write_child_parks_parent_and_approval_resumes(client):
    parent = _write_child_orchestration(client)
    assert parent["status"] == "awaiting_approval", parent
    report = _child(client, parent["id"], "report")
    assert report["status"] == "awaiting_approval"
    assert report["id"] in parent["draft"]
    gather = _child(client, parent["id"], "gather")
    assert gather["status"] == "completed"
    assert "alpha" in gather["draft"]
    # The wrap step has not run yet — the parent is parked.
    assert _children(client, parent["id"]).__len__() == 2

    # SoD inherited: the orchestration's creator cannot approve the child.
    denied = client.post(f"/api/runs/{report['id']}/approve", json={})
    assert denied.status_code == 403, denied.text
    parent = client.get(f"/api/runs/{parent['id']}").json()
    assert parent["status"] == "awaiting_approval"

    # A separate approver approves the child -> parent resumes + completes.
    out = client.post(f"/api/runs/{report['id']}/approve", json={},
                      headers=APPROVER)
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "applied"
    parent = client.get(f"/api/runs/{parent['id']}").json()
    assert parent["status"] == "completed", parent
    wrap = _child(client, parent["id"], "wrap")
    assert wrap["status"] == "completed"
    # Synthesis mentions the child results (report content flowed through).
    assert "[gather]" in parent["draft"] and "[report]" in parent["draft"]
    assert "alpha" in parent["draft"]
    assert client.get("/api/audit/verify").json()["intact"] is True


def test_child_rejection_fails_parent_with_reason(client):
    parent = _write_child_orchestration(client)
    report = _child(client, parent["id"], "report")
    out = client.post(f"/api/runs/{report['id']}/reject",
                      json={"reason": "numbers look wrong"},
                      headers=APPROVER)
    assert out.status_code == 200, out.text
    parent = client.get(f"/api/runs/{parent['id']}").json()
    assert parent["status"] == "failed", parent
    assert "report" in parent["error"] and "rejected" in parent["error"]
    assert "numbers look wrong" in parent["error"]
    assert _child(client, parent["id"], "report")["status"] == "rejected"
    types = [e["event_type"] for e in _events(client)]
    assert "orchestration_failed" in types
    assert client.get("/api/audit/verify").json()["intact"] is True


def test_parent_cannot_be_approved_directly(client):
    parent = _write_child_orchestration(client)
    resp = client.post(f"/api/runs/{parent['id']}/approve", json={},
                       headers=APPROVER)
    assert resp.status_code == 409, resp.text
    assert "child" in resp.json()["detail"]


# ---- validation ------------------------------------------------------------------

def test_cycle_is_422(client):
    agent = add_agent(client, [], skills=("data-lookup",))
    subtasks = [
        {"key": "a", "skill": "data-lookup", "input": "x",
         "depends_on": ["b"]},
        {"key": "b", "skill": "data-lookup", "input": "y",
         "depends_on": ["a"]},
    ]
    resp = _orchestrate(client, agent["id"], subtasks=subtasks)
    assert resp.status_code == 422, resp.text
    assert client.get("/api/runs").json() == []


def test_unknown_depends_on_is_422(client):
    agent = add_agent(client, [], skills=("data-lookup",))
    subtasks = [{"key": "a", "skill": "data-lookup", "input": "x",
                 "depends_on": ["ghost"]}]
    resp = _orchestrate(client, agent["id"], subtasks=subtasks)
    assert resp.status_code == 422, resp.text


def test_duplicate_key_and_disallowed_skill_are_422(client):
    agent = add_agent(client, [], skills=("data-lookup",))
    dup = [{"key": "a", "skill": "data-lookup", "input": "1"},
           {"key": "a", "skill": "data-lookup", "input": "2"}]
    assert _orchestrate(client, agent["id"], subtasks=dup).status_code == 422
    bad = [{"key": "a", "skill": "draft-report", "input": "1"}]
    resp = _orchestrate(client, agent["id"], subtasks=bad)
    assert resp.status_code == 422, resp.text
    assert "allow-list" in resp.json()["detail"]


def test_child_agent_owned_by_someone_else_is_403_and_audited(client):
    mine = add_agent(client, [], skills=("data-lookup",))
    # An agent owned by another user, created by that owner.
    bob = H("bob@example.com", "owner")
    other = client.post("/api/agents", json={
        "name": "Bob's agent", "owner": "bob@example.com",
        "allowed_skills": ["data-lookup"],
    }, headers=bob)
    assert other.status_code == 201, other.text
    alice = H("alice@example.com", "owner")
    lead = client.post("/api/agents", json={
        "name": "Alice lead", "owner": "alice@example.com",
        "allowed_skills": ["data-lookup"],
    }, headers=alice).json()
    subtasks = [{"key": "a", "agent_id": other.json()["id"],
                 "skill": "data-lookup", "input": "peek"}]
    resp = _orchestrate(client, lead["id"], subtasks=subtasks,
                        headers=alice)
    assert resp.status_code == 403, resp.text
    denied = [e for e in _events(client, "authz_denied")
              if e["actor"] == "alice@example.com"]
    assert denied, "ownership violation must be audited as authz_denied"
    # No parent (or child) run was created by the denied request.
    assert client.get("/api/runs").json() == []
    assert mine["owner"] == "ops-lead@example.com"


def test_auditor_and_approver_cannot_orchestrate(client):
    agent = add_agent(client, [], skills=("data-lookup",))
    for headers in (H("aud@example.com", "auditor"),
                    H("appr@example.com", "approver")):
        resp = _orchestrate(client, agent["id"],
                            subtasks=[{"key": "a", "skill": "data-lookup",
                                       "input": "x"}],
                            headers=headers)
        assert resp.status_code == 403, resp.text
    assert client.get("/api/runs").json() == []
