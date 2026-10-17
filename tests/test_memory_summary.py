"""Governed memory summarization (v1.2): a summary is a derived view —
drafted by a run, reviewer-gated, stored only on named approval by
someone other than the creator, with provenance to its sources. The
sources themselves are never modified."""
import pytest


def H(actor, role=None):
    headers = {"X-Actor": actor}
    if role is not None:
        headers["X-Role"] = role
    return headers


APPROVER = H("approver@example.com", "approver")


def _agent(client, owner="admin@example.com", skills=("memory-summary",)):
    resp = client.post("/api/agents", json={
        "name": "Memory Agent", "owner": owner,
        "allowed_skills": list(skills),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _fact(client, content, tier="private", owner=None, headers=None):
    body = {"tier": tier, "content": content}
    if owner is not None:
        body["owner"] = owner
    resp = client.post("/api/memory", json=body, headers=headers or {})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _summarize(client, agent_id, headers=None, **kw):
    body = {"agent_id": agent_id, "tier": "private"}
    body.update(kw)
    return client.post("/api/memory/summarize", json=body,
                       headers=headers or {})


def _summaries(client, tier="private"):
    return [m for m in client.get(f"/api/memory?tier={tier}").json()
            if m["kind"] == "summary"]


# ---- cataloguing + certification -------------------------------------------

def test_skill_catalogued_and_eval_covers_it(client):
    names = [s["name"] for s in client.get("/api/skills").json()]
    assert "memory-summary" in names
    client.put("/api/settings/model", json={"model": ""})
    results = client.post("/api/settings/model/evaluate").json()["results"]
    by_skill = {r["skill"]: r for r in results}
    assert "memory-summary" in by_skill
    assert by_skill["memory-summary"]["passed"] is True
    assert by_skill["memory-summary"]["placeholder"] is True


# ---- happy path --------------------------------------------------------------

def test_private_summary_happy_path(client):
    facts = [_fact(client, f"fact number {i}: the user prefers option {i}")
             for i in range(3)]
    fact_ids = [f["id"] for f in facts]
    agent = _agent(client)
    resp = _summarize(client, agent["id"])
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["status"] == "awaiting_approval", run
    assert run["model_placeholder"] is True
    assert run["params"]["source_ids"] == fact_ids
    assert "Digest of 3" in run["draft"]

    # Nothing stored before approval; sources untouched.
    items = client.get("/api/memory?tier=private").json()
    assert len(items) == 3 and all(m["kind"] == "fact" for m in items)

    out = client.post(f"/api/runs/{run['id']}/approve", json={},
                      headers=APPROVER)
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "applied"

    summaries = _summaries(client)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["tier"] == "private" and s["owner"] == "admin@example.com"
    assert s["source_ids"] == fact_ids, "provenance: exactly the sources"
    assert s["generated_by_run_id"] == run["id"]
    assert s["model"] == "test-model (placeholder)"
    assert s["content"] == run["draft"]
    # Sources are still there, byte-identical (derived view, not a move).
    after = {m["id"]: m for m in client.get("/api/memory?tier=private").json()}
    for f in facts:
        assert after[f["id"]]["content"] == f["content"]
        assert after[f["id"]]["kind"] == "fact"
    # Audit tells the whole story and the chain verifies.
    events = client.get("/api/audit").json()
    types = [e["event_type"] for e in events]
    for expected in ("memory_summary_requested", "memory_summary_drafted",
                     "memory_summary_applied"):
        assert expected in types, f"missing {expected}; got {types}"
    applied = [e for e in events
               if e["event_type"] == "memory_summary_applied"][0]
    assert "3 source(s)" in applied["summary"]
    assert client.get("/api/audit/verify").json()["intact"] is True


def test_summaries_are_not_resummarized_by_default(client):
    for i in range(2):
        _fact(client, f"base fact {i}")
    agent = _agent(client)
    run1 = _summarize(client, agent["id"]).json()
    client.post(f"/api/runs/{run1['id']}/approve", json={}, headers=APPROVER)
    run2 = _summarize(client, agent["id"]).json()
    assert run2["status"] == "awaiting_approval", run2
    assert len(run2["params"]["source_ids"]) == 2, \
        "latest-N selection covers facts only, not the earlier summary"


def test_explicit_source_subset(client):
    a = _fact(client, "alpha fact")
    _fact(client, "beta fact")
    c = _fact(client, "gamma fact")
    agent = _agent(client)
    run = _summarize(client, agent["id"],
                     source_item_ids=[c["id"], a["id"]]).json()
    assert run["params"]["source_ids"] == [c["id"], a["id"]]


# ---- gates -------------------------------------------------------------------

def test_creator_cannot_approve_own_summary(client):
    alice = H("alice@example.com", "owner")
    _fact(client, "alice likes dark mode", headers=alice)
    agent = _agent(client, owner="alice@example.com")
    run = _summarize(client, agent["id"], headers=alice).json()
    assert run["status"] == "awaiting_approval", run
    assert client.post(f"/api/runs/{run['id']}/approve", json={},
                       headers=H("alice@example.com", "approver")
                       ).status_code == 403
    assert _summaries(client) == [], "SoD denial must store nothing"
    out = client.post(f"/api/runs/{run['id']}/approve", json={},
                      headers=APPROVER)
    assert out.status_code == 200
    assert len(_summaries(client)) == 1


def test_rejection_stores_nothing(client):
    _fact(client, "a fact")
    agent = _agent(client)
    run = _summarize(client, agent["id"]).json()
    out = client.post(f"/api/runs/{run['id']}/reject",
                      json={"reason": "not yet"}, headers=APPROVER)
    assert out.json()["status"] == "rejected"
    assert _summaries(client) == []


def test_reviewer_veto_stores_nothing(client):
    _fact(client, "note: leaked key sk-0123456789abcdef in the vault doc")
    agent = _agent(client)
    run = _summarize(client, agent["id"]).json()
    assert run["status"] == "vetoed", run
    assert run["review"]["approved"] is False
    assert _summaries(client) == []


def test_team_summary(client):
    _fact(client, "deploys happen on Tuesdays", tier="team",
          owner="platform-team")
    _fact(client, "on-call rota is in the wiki", tier="team",
          owner="platform-team")
    agent = _agent(client)
    resp = _summarize(client, agent["id"], tier="team",
                      owner="platform-team")
    run = resp.json()
    assert run["status"] == "awaiting_approval", run
    client.post(f"/api/runs/{run['id']}/approve", json={}, headers=APPROVER)
    summaries = _summaries(client, tier="team")
    assert len(summaries) == 1
    assert summaries[0]["owner"] == "platform-team"
    assert len(summaries[0]["source_ids"]) == 2


def test_org_summary_admin_only(client):
    _fact(client, "travel policy v3", tier="org", owner="org")
    agent = _agent(client, owner="bob@example.com")
    bob = H("bob@example.com", "owner")
    resp = _summarize(client, agent["id"], headers=bob, tier="org")
    assert resp.status_code == 403, resp.text
    denied = [e for e in client.get("/api/audit?event_type=authz_denied").json()
              if e["actor"] == "bob@example.com"]
    assert denied, "org draft denial must be audited as authz_denied"
    # Admin can draft it; a different approver stores it.
    agent2 = _agent(client)
    run = _summarize(client, agent2["id"], tier="org").json()
    assert run["status"] == "awaiting_approval", run
    client.post(f"/api/runs/{run['id']}/approve", json={}, headers=APPROVER)
    assert len(_summaries(client, tier="org")) == 1


def test_private_summary_of_someone_elses_items_denied(client):
    alice_item = _fact(client, "alice private note",
                       headers=H("alice@example.com", "user"))
    agent = _agent(client, owner="bob@example.com")
    bob = H("bob@example.com", "user")
    # Bob scopes to his own (empty) private memory but names Alice's id.
    resp = _summarize(client, agent["id"], headers=bob,
                      owner="bob@example.com",
                      source_item_ids=[alice_item["id"]])
    assert resp.status_code == 403, resp.text
    # And targeting Alice's private scope directly is denied too.
    resp = _summarize(client, agent["id"], headers=bob,
                      owner="alice@example.com")
    assert resp.status_code == 403, resp.text


def test_cross_tier_source_rejected(client):
    team_item = _fact(client, "team thing", tier="team", owner="t1")
    agent = _agent(client)
    resp = _summarize(client, agent["id"], tier="private",
                      source_item_ids=[team_item["id"]])
    assert resp.status_code == 403, resp.text


def test_auditor_cannot_draft_summary(client):
    _fact(client, "a fact")
    agent = _agent(client)
    resp = _summarize(client, agent["id"],
                      headers=H("auditor@example.com", "auditor"))
    assert resp.status_code == 403, resp.text
    assert client.get("/api/runs").json() == []


def test_agent_without_skill_allowlist_fails_closed(client):
    _fact(client, "a fact")
    agent = _agent(client, skills=("data-lookup",))
    run = _summarize(client, agent["id"]).json()
    assert run["status"] == "failed", run
    assert "memory-summary" in run["error"]
    assert _summaries(client) == []


def test_empty_scope_is_422(client):
    agent = _agent(client)
    resp = _summarize(client, agent["id"], tier="team", owner="no-such-team")
    assert resp.status_code == 422, resp.text
