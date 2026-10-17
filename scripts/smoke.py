#!/usr/bin/env python3
"""Live smoke: walks the full demo flow over HTTP against a running server.

Usage: python scripts/smoke.py [base_url]   (default http://127.0.0.1:8090)
Expects a fresh server (TestModel placeholder, demo auth).

RBAC (v1.1) is exercised along the way: setup actions run as the demo
admin, an auditor's run attempt is denied (403, audited), the run's own
creator is denied approval (separation of duties), and a different
named approver with the approver role performs the approval.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
ADMIN = {"X-Actor": "admin@example.com"}  # demo default actor -> admin role
APPROVER = {"X-Actor": "approver@example.com", "X-Role": "approver"}
AUDITOR = {"X-Actor": "auditor@example.com", "X-Role": "auditor"}


def call(method, path, body=None, headers=None, expect=None):
    hdrs = {"Content-Type": "application/json", **(headers or ADMIN)}
    req = urllib.request.Request(
        BASE + path, method=method, headers=hdrs,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload = resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, {}
    if expect is None:
        assert 200 <= status < 300, f"{method} {path} -> {status}: {payload}"
    else:
        assert status == expect, f"{method} {path} -> {status} (expected {expect}): {payload}"
    return payload


def main():
    health = call("GET", "/api/health")
    assert health["ok"], health
    print("health ok; model:", health["model"])

    me = call("GET", "/api/me")
    assert me["role"] == "admin", me
    print("caller:", me)

    call("PUT", "/api/settings/model", {"model": ""})
    results = call("POST", "/api/settings/model/evaluate")["results"]
    assert results and all(r["passed"] and r["placeholder"] for r in results)
    print("model eval (placeholder):", [(r["skill"], r["passed"]) for r in results])

    # RBAC: a non-admin cannot set the model; the denial is audited.
    call("PUT", "/api/settings/model", {"model": "openai:gpt-4o"},
         headers={"X-Actor": "owner@example.com", "X-Role": "owner"}, expect=403)
    print("rbac: owner denied model set (403)")

    conn = call("POST", "/api/connectors",
                {"catalog_id": "postgres", "config": {"standin_name": "demo"}})
    health = call("POST", f"/api/connectors/{conn['id']}/test")
    assert health["ok"] and health["live"]
    print("connector live:", health["detail"])

    agent = call("POST", "/api/agents", {
        "name": "Ops Assistant", "owner": "ops-lead@example.com",
        "allowed_connectors": [conn["id"]],
        "allowed_skills": ["daily-brief", "data-lookup", "draft-report",
                           "memory-summary"],
    })

    run_body = {
        "agent_id": agent["id"], "skill": "data-lookup", "input": "list records",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    }
    # RBAC: an auditor may not run agents at all.
    call("POST", "/api/runs", run_body, headers=AUDITOR, expect=403)
    print("rbac: auditor denied run creation (403)")

    run = call("POST", "/api/runs", run_body)
    assert run["status"] == "completed" and "alpha" in run["draft"], run
    print("data-lookup run:", run["id"], run["status"])

    run = call("POST", "/api/runs", {
        "agent_id": agent["id"], "skill": "draft-report", "input": "Weekly status",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    })
    assert run["status"] == "awaiting_approval", run
    print("draft-report run:", run["id"], "-> awaiting approval (reviewer approved)")

    # Separation of duties: the run's creator (the admin) cannot approve it.
    call("POST", f"/api/runs/{run['id']}/approve", {}, expect=403)
    print("rbac: creator denied own approval (403, separation of duties)")

    # A different named approver, acting in the approver role, approves.
    applied = call("POST", f"/api/runs/{run['id']}/approve",
                   {"approver": "approver@example.com"}, headers=APPROVER)
    assert applied["status"] == "applied", applied
    print("approved & applied:", applied["applied_note"])

    entries = call("GET", "/api/audit")
    types = sorted({e["event_type"] for e in entries})
    print("audit events:", types)
    assert "authz_denied" in types, "denied attempts must be audited"
    verify = call("GET", "/api/audit/verify")
    assert verify["intact"], verify
    print("audit chain intact:", verify)

    pack = call("GET", f"/api/runs/{run['id']}/evidence")
    assert pack["chain_verification"]["intact"] and pack["approvals"]
    print("evidence pack ok:", run["id"])

    # --- governed memory summarization (v1.2) -------------------------
    facts = [call("POST", "/api/memory",
                  {"tier": "private", "content": c})
             for c in ("Deploys happen on Tuesdays at 10:00.",
                       "The staging database is refreshed nightly.",
                       "Priya owns the release checklist.")]
    srun = call("POST", "/api/memory/summarize",
                {"agent_id": agent["id"], "tier": "private"})
    assert srun["status"] == "awaiting_approval", srun
    assert srun["model_placeholder"] is True, srun
    print("memory summary drafted (placeholder):", srun["id"],
          "sources:", len(srun["params"]["source_ids"]))

    items = call("GET", "/api/memory?tier=private")
    assert not any(m["kind"] == "summary" for m in items), \
        "summary must not be stored before approval"

    # Separation of duties applies to summary approvals too.
    call("POST", f"/api/runs/{srun['id']}/approve", {}, expect=403)
    print("rbac: summary creator denied own approval (403)")

    applied = call("POST", f"/api/runs/{srun['id']}/approve",
                   {"approver": "approver@example.com"}, headers=APPROVER)
    assert applied["status"] == "applied", applied
    items = call("GET", "/api/memory?tier=private")
    summaries = [m for m in items if m["kind"] == "summary"]
    assert len(summaries) == 1, items
    summary = summaries[0]
    assert summary["source_ids"] == [f["id"] for f in facts], summary
    assert summary["generated_by_run_id"] == srun["id"]
    assert len([m for m in items if m["kind"] == "fact"]) == 3, \
        "sources must remain (summary is a derived view)"
    print("memory summary stored:", summary["id"],
          "from", len(summary["source_ids"]), "sources; originals intact")

    entries = call("GET", "/api/audit")
    types = sorted({e["event_type"] for e in entries})
    for expected in ("memory_summary_requested", "memory_summary_drafted",
                     "memory_summary_applied"):
        assert expected in types, f"missing audit event {expected}"
    verify = call("GET", "/api/audit/verify")
    assert verify["intact"], verify
    print("audit chain still intact:", verify)
    print("SMOKE PASSED")


if __name__ == "__main__":
    main()
