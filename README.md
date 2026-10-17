# Open Agent Plane

**One agent per person, governed as one platform.**

A vendor-neutral, BYO-model, self-hosted **agent control plane** for the
enterprise. Employees get named, persistent agents; the organization gets
one governance layer for all of them: a governed connector catalog, an
agent registry, reviewer + human approval gates on every write, and an
append-only, hash-chained audit trail that can prove who did what, with
which model, through which connector, approved by whom.

> **We don't sell you a model. Bring yours.**
> Set one value — `AGENTPLANE_MODEL` — to any Pydantic AI model string
> (OpenAI, Azure OpenAI, Anthropic, AWS Bedrock, Google, Ollama/local, or
> an OpenAI-compatible endpoint). Your model runs on your route, at your
> rates, under your data rules. Swap the model, keep everything else:
> registry, skills, memory, approvals, and audit history don't move.
> A built-in **certification gate** runs a small eval suite per skill; a
> model must pass before production agents may use it, and swapping the
> model re-triggers the eval.

Unset model = an offline **TestModel placeholder** so the whole demo runs
with zero keys. Placeholder runs and evals are labelled as placeholder —
never presented as real model behavior or real certification.

## Quickstart

```bash
git clone https://github.com/revanthsamavedam/open-agent-plane.git
cd open-agent-plane
cp .env.example .env          # optionally set AGENTPLANE_MODEL in .env
docker compose up --build     # or: see "Without Docker" below
open http://localhost:8090    # the setup wizard walks you through the rest
```

Without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:build_app --factory --port 8090
```

## The demo flow (2 minutes, no keys needed)

In the UI's **Setup wizard**:

1. **Model** — leave blank (TestModel placeholder) or paste your model
   string, save, and run the certification eval.
2. **Connector** — pick *Postgres (read-only)* from the catalog, config
   `{"standin_name": "demo"}`, add it. (LIVE connectors run against local
   stand-ins; SCAFFOLD connectors are labelled honestly — see
   [CONNECTORS.md](CONNECTORS.md).)
3. **Agent** — create your first agent with a named human owner.
4. **Run** — run `data-lookup` with
   `{"query": "SELECT title FROM records ORDER BY id"}` and watch it finish
   with real rows. Then run `draft-report`: it goes to the reviewer, lands
   in **Approvals**, a named human approves it, and only then is it applied.
5. **Audit** — open the Audit page: every step above is there. Click
   *Verify chain*, export JSON/CSV, or download a per-run **Evidence Pack**
   from the Dashboard.

The same flow is scripted in `scripts/smoke.py` and covered end to end by
the pytest suite.

## What's inside

| Area | Where |
|---|---|
| Agent registry + runs (reviewer veto, approval-gated writes) | `app/service.py`, `app/main.py` |
| **RBAC (v1.1): admin / owner / approver / auditor / user roles, ownership-scoped runs, separation of duties on approvals, audited denials** | `app/auth.py`, `app/main.py`, [SECURITY.md](SECURITY.md) |
| BYO model + certification eval gate | `app/gateway.py` |
| Connector catalog (19 common enterprise connectors, YAML-driven) | `connectors/`, `app/connectors.py`, [CONNECTORS.md](CONNECTORS.md) |
| Skills (versioned SKILL.md: daily-brief, data-lookup, draft-report) | `skills/` |
| **Audit: append-only, hash-chained, exportable, tamper-evident** | `app/store.py`, [AUDIT.md](AUDIT.md) |
| Memory: private / team (curated, approved promotion) / org tiers | `app/main.py` |
| Auth: `AUTH_MODE=demo` or `jwt` (OIDC) + RBAC roles; no silent demo fallback | `app/auth.py`, [SECURITY.md](SECURITY.md) |
| Architecture (from the Enterprise Muse blueprint) | [ARCHITECTURE.md](ARCHITECTURE.md) |

## Design rules

- **Agents never hold credentials.** Connector config stores secret
  *references* only (`env:`, `vault:`, `secret:`); adapters resolve them
  service-side and return only results.
- **Writes are drafts until a named human approves them.** Reviewer first,
  human second, apply last — all audited. And the approver is never the
  person who created the run: RBAC roles (admin / owner / approver /
  auditor / user) with separation of duties are enforced server-side,
  and every denial lands in the audit trail as `authz_denied`.
- **The audit trail is the product.** If it isn't in the audit trail, it
  didn't happen.
- **Honest labels.** A connector is LIVE only when its adapter really
  performs the read/write (against a stand-in locally). Everything else is
  SCAFFOLD, in the README, the catalog, and the UI.

## Tests

```bash
pytest -q
```

Covers: connector contract tests for every catalog entry, the full demo
flow over HTTP, audit-chain verification **including a tamper test**,
persistence across store reopen, JWT/demo auth modes, memory promotion,
the model certification gate, and the **RBAC matrix** (role gates,
ownership-scoped runs, separation of duties, audited denials, JWT role
claims).

## License

MIT — see [LICENSE](LICENSE).
