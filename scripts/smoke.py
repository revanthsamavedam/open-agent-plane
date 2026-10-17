#!/usr/bin/env python3
"""Live smoke: walks the full demo flow over HTTP against a running server.

Usage: python scripts/smoke.py [base_url]   (default http://127.0.0.1:8090)
Expects a fresh server (TestModel placeholder, demo auth).
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
HEADERS = {"Content-Type": "application/json", "X-Actor": "admin@example.com"}


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method, headers=HEADERS,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def main():
    health = call("GET", "/api/health")
    assert health["ok"], health
    print("health ok; model:", health["model"])

    call("PUT", "/api/settings/model", {"model": ""})
    results = call("POST", "/api/settings/model/evaluate")["results"]
    assert results and all(r["passed"] and r["placeholder"] for r in results)
    print("model eval (placeholder):", [(r["skill"], r["passed"]) for r in results])

    conn = call("POST", "/api/connectors",
                {"catalog_id": "postgres", "config": {"standin_name": "demo"}})
    health = call("POST", f"/api/connectors/{conn['id']}/test")
    assert health["ok"] and health["live"]
    print("connector live:", health["detail"])

    agent = call("POST", "/api/agents", {
        "name": "Ops Assistant", "owner": "ops-lead@example.com",
        "allowed_connectors": [conn["id"]],
        "allowed_skills": ["daily-brief", "data-lookup", "draft-report"],
    })

    run = call("POST", "/api/runs", {
        "agent_id": agent["id"], "skill": "data-lookup", "input": "list records",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    })
    assert run["status"] == "completed" and "alpha" in run["draft"], run
    print("data-lookup run:", run["id"], run["status"])

    run = call("POST", "/api/runs", {
        "agent_id": agent["id"], "skill": "draft-report", "input": "Weekly status",
        "connector_id": conn["id"], "action": "query",
        "params": {"query": "SELECT title FROM records ORDER BY id"},
    })
    assert run["status"] == "awaiting_approval", run
    print("draft-report run:", run["id"], "-> awaiting approval (reviewer approved)")

    applied = call("POST", f"/api/runs/{run['id']}/approve",
                   {"approver": "ops-lead@example.com"})
    assert applied["status"] == "applied", applied
    print("approved & applied:", applied["applied_note"])

    entries = call("GET", "/api/audit")
    types = sorted({e["event_type"] for e in entries})
    print("audit events:", types)
    verify = call("GET", "/api/audit/verify")
    assert verify["intact"], verify
    print("audit chain intact:", verify)

    pack = call("GET", f"/api/runs/{run['id']}/evidence")
    assert pack["chain_verification"]["intact"] and pack["approvals"]
    print("evidence pack ok:", run["id"])
    print("SMOKE PASSED")


if __name__ == "__main__":
    main()
