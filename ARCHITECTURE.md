# Architecture

Open Agent Plane implements the Enterprise Muse blueprint: *one agent per
person, governed as one platform.* Personal agents flip to per-employee
agents plus shared team agents, under one governance plane.

## Layers

```
┌─ Experience ────────────────────────────────────────────────────────┐
│ Single static ops UI served by the API: setup wizard, dashboard /   │
│ runs, agents, connectors, approvals queue, audit viewer, settings.  │
│ (In a deployment this is also where chat, Teams/Slack, IDE, and a   │
│  ticket front door plug in — the API is the same.)                  │
├─ Agents ────────────────────────────────────────────────────────────┤
│ Agent registry: named agents, each with a named human owner, an     │
│ allow-list of connectors and skills, and an optional model          │
│ override. Runs are executed by a Pydantic AI agent loop; a          │
│ reviewer gate can veto a draft before a human ever sees it.         │
│ Skills are versioned SKILL.md definitions (daily-brief,             │
│ data-lookup, draft-report).                                         │
├─ Governed connector layer ──────────────────────────────────────────┤
│ Config-driven catalog (YAML per connector — see CONNECTORS.md).     │
│ Adapters run service-side behind a common protocol and return       │
│ results only. Read paths for data connectors enforce read-only      │
│ access; writes are drafts until approved.                           │
├─ Data ──────────────────────────────────────────────────────────────┤
│ SQLite by default (zero-setup self-host). DATABASE_URL is the seam  │
│ for Postgres in a deployment. Local stand-ins for LIVE connectors.  │
└─────────────────────────────────────────────────────────────────────┘
   Side planes: Identity & credentials · Memory · Audit (below)
```

## Identity & credential model

- Caller identity is threaded through every run and audit entry.
  `AUTH_MODE=demo` takes the actor from a header for local demos;
  `AUTH_MODE=jwt` verifies an OIDC/JWT bearer token (Entra ID, Okta, or
  Google in production — this reference build verifies HS256 with
  `JWT_SECRET`; swap in your IdP's JWKS). Missing JWT config is a startup
  error, never a silent demo fallback. Auth failures are audited.
- Connector credentials are **references only** (`env:`, `vault:`,
  `secret:`), validated at configuration time; raw-looking secrets are
  rejected. Adapters resolve references service-side. Agents — and the
  model — never see credential values.
- Callers act in one of five RBAC roles (admin, owner, approver,
  auditor, user), resolved from JWT role claims or, in demo mode, the
  `X-Role` header. The server enforces the permission matrix (model
  and connector administration are admin-only; runs and agent creation
  are ownership-scoped for non-admins) and separation of duties on
  approvals and memory promotion; every denial is itself an
  `authz_denied` audit event. Full matrix: [SECURITY.md](SECURITY.md).

## Memory (3 tiers)

- **Private** — per user; never leaves the person.
- **Team** — curated; content arrives only via human-approved promotion
  from a private item (the promotion is audited).
- **Org** — versioned, read-only reference material (policies, runbooks).

### Governed summarization

Memory items are `kind="fact"` or `kind="summary"`. A summary is a
**derived view, never a replacement**: it is produced by the
`memory-summary` skill through the ordinary governed write path — a run
drafts it (`POST /api/memory/summarize`), the reviewer can veto it, and
a named human approver (never the run's creator) applies it. Only then
is the summary stored, with provenance on the item itself:
`source_ids` (every source item), `generated_by_run_id`, and the model
label. Originals are never modified or deleted, and default source
selection covers facts only, so summaries are not silently re-summarized.
Scope gates mirror the tiers: private summaries are limited to the
caller's own items (admins excepted), org summaries are admin-only to
draft, and cross-scope source ids are denied and audited. With the
offline placeholder model, the digest is deterministic and extractive
and the run is labelled placeholder; with a real model, the memory-
summary skill is behind the same per-skill certification gate as every
other skill. Requested / drafted / applied are distinct audit events.

## Writes

`draft → reviewer verdict → named human approval → applied`. Rejection
ends the run. For LIVE connectors, applying performs the write against the
stand-in; for SCAFFOLD connectors, applying records the intended write in
the run + audit, labelled as recorded intent. Every step is an audit event.

## Orchestration (subagents)

A run can be a **plan**: `POST /api/orchestrate` creates a parent run
(skill `orchestrate`) whose subtasks execute as **child runs** — each
child is a first-class run through the same pipeline (certification
gate, connector calls, reviewer verdict, approval gate), linked to the
parent by `parent_run_id` and its `plan_step` key. There is no separate
subagent runtime to govern: governance is *inherited* by construction.

- **Plans** are DAGs (unique keys, existing `depends_on` references,
  acyclic — validated up front, 422 otherwise). Subtasks may be
  explicit, or proposed by a planner through the model gateway; the
  offline placeholder model yields a deterministic placeholder plan,
  labelled on the parent run.
- **Handoffs are typed by template**: a child's input may embed
  `{{key.result}}`, resolved at execution time from that dependency's
  result. Unresolvable templates fail the child and the parent — never
  a silent substitution.
- **Approvals park the plan**: a child write reaching
  `awaiting_approval` parks the parent (its draft names the waiting
  child). Approving the child resumes the parent; rejecting or vetoing
  it fails the parent with the reason recorded. Separation of duties
  is inherited — children are created by the orchestration's creator,
  who can therefore never approve them.
- **The parent result is a synthesis** of child result summaries
  (deterministic digest by default; a real model may write it,
  labelled). Orchestration and subtask started/finished/failed are
  audit events with parent + child ids, and the parent's Evidence
  Pack includes the child runs.

## Audit design

Append-only, hash-chained entries in the same store: each entry carries
the previous entry's hash and its own hash over its canonical contents, so
any later edit to a stored entry breaks the chain and is detected by
verification (there is a test that tampers with a stored entry and proves
detection). There is deliberately no update/delete API for audit entries.
Full taxonomy and export formats: [AUDIT.md](AUDIT.md).

## BYO model

The model is one setting (`AGENTPLANE_MODEL` or Settings) passed through
to Pydantic AI — OpenAI, Azure OpenAI, Anthropic, Bedrock, Google, Ollama,
or an OpenAI-compatible endpoint. A certification eval (construction,
responding, tool-calling) runs per skill; production runs on a real model
require a passing, non-placeholder eval for that skill, and changing the
model string re-triggers the requirement. The offline TestModel
placeholder keeps demos and CI keyless and is always labelled.
