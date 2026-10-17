#!/usr/bin/env python3
"""Deploy consumer/ to Vercel project 'agentplane-personal' via the
connected Vercel MCP wrapper (/opt/hatch/bin/vercel). No secrets in this
file — auth lives in the connector. Flow: get_project -> create_project
-> upload_file -> create_deployment -> poll get_deployment. teamId is
OMITTED everywhere (passing it to the MCP tools 403s).
"""
import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = "agentplane-personal"
HERE = Path(__file__).resolve().parent
FILES = ["index.html", "app.js", "styles.css",
         "api/index.py", "requirements.txt", "vercel.json"]


def call(tool, args):
    p = subprocess.run(
        ["/opt/hatch/bin/vercel", "call-tool", "--name", tool,
         "--arguments-json", json.dumps(args)],
        capture_output=True, text=True, timeout=300,
    )
    out = p.stdout.strip()
    try:
        d = json.loads(out)
    except Exception:
        return {"_raw": out, "_stderr": p.stderr[-500:]}
    try:
        inner = d["result"]["content"][0]["text"]
        return json.loads(inner)
    except Exception:
        return d


def dig(d, *keys):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
    return d


def main():
    proj = call("get_project", {"idOrName": PROJECT})
    pid = dig(proj, "result", "id") or dig(proj, "id")
    if not pid:
        print("project not found, creating…")
        proj = call("create_project", {
            "requestBody": {"name": PROJECT, "framework": None},
        })
        pid = dig(proj, "result", "id") or dig(proj, "id")
        if not pid:
            print("create_project response:", json.dumps(proj)[:800])
            sys.exit("could not create project")
    print("project id:", pid)

    refs = []
    for name in FILES:
        data = (HERE / name).read_bytes()
        sha = hashlib.sha1(data).hexdigest()
        up = call("upload_file", {
            "xVercelDigest": sha,
            "contentLength": len(data),
            "requestBody": base64.b64encode(data).decode(),
        })
        print("uploaded", name, sha[:10], "->", json.dumps(up)[:160])
        refs.append({"file": name, "sha": sha, "size": len(data)})

    dep = call("create_deployment", {
        "forceNew": "1",
        "requestBody": {
            "name": PROJECT,
            "files": refs,
            "target": "production",
            "projectSettings": {"framework": None},
        },
    })
    dep_id = (dig(dep, "result", "deployment", "id") or dig(dep, "result", "id")
              or dig(dep, "id"))
    dep_url = (dig(dep, "result", "deployment", "url") or dig(dep, "result", "url")
               or dig(dep, "url"))
    if not dep_id:
        print("create_deployment response:", json.dumps(dep)[:1200])
        sys.exit("deployment failed to start")
    print("deployment:", dep_id, dep_url)

    for _ in range(36):
        time.sleep(5)
        d = call("get_deployment", {"idOrUrl": dep_id})
        state = (dig(d, "result", "deployment", "readyState")
                 or dig(d, "result", "readyState") or dig(d, "readyState"))
        url = (dig(d, "result", "deployment", "url") or dig(d, "result", "url")
               or dig(d, "url") or dep_url)
        print("state:", state, url)
        if state == "READY":
            print("DEPLOYMENT_URL=https://" + url)
            print(f"ALIAS_URL=https://{PROJECT}.vercel.app")
            return
        if state in ("ERROR", "CANCELED"):
            ev = call("list_deployment_events", {"id": dep_id})
            print("deployment events tail:", json.dumps(ev)[:2000])
            sys.exit("deployment errored")
    sys.exit("timed out waiting for deployment")


if __name__ == "__main__":
    main()
