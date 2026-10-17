# Audit

Audit is a first-class feature, not a log file.

## Design

- **Append-only.** Entries are only ever appended through one code path.
  There is no update or delete API for audit entries — in the HTTP API, in
  the store, or in the UI.
- **Hash-chained.** Every entry stores `prev_hash` (the previous entry's
  hash, or `GENESIS`) and `entry_hash` (SHA-256 over the previous hash and
  the entry's canonical contents). Editing, deleting, or reordering a
  stored entry breaks the chain.
- **Verifiable.** `GET /api/audit/verify` (and the Audit page's *Verify
  chain* button) recomputes the whole chain and reports intact, or the
  sequence number where it breaks. A test tampers with a stored entry in
  the database and proves verification detects it.

## Event taxonomy

| Event | When |
|---|---|
| `model_set` | Model setting changed |
| `model_eval` | Certification eval ran (pass/fail, placeholder flagged) |
| `model_certified` | A real (non-placeholder) model passed the eval |
| `connector_added` | A connector instance was configured |
| `connector_tested` | A connector health check ran |
| `agent_created` | An agent was registered |
| `run_started` / `run_finished` / `run_failed` | Run lifecycle |
| `tool_call` | Every connector call: connector, action, agent, run, actor |
| `reviewer_verdict` | Reviewer approved or vetoed a draft |
| `approval` / `rejection` | Named human decision, with the approver's name |
| `memory_added` / `memory_promoted` | Memory writes and tier promotions |
| `memory_summary_requested` / `memory_summary_drafted` / `memory_summary_applied` | Governed memory summarization: run created, draft composed, and (only on named approval) the summary item stored with its source count |
| `orchestration_started` / `orchestration_finished` / `orchestration_failed` | Orchestration parent run lifecycle (plan size + source on start; failure reason on fail) |
| `subtask_started` / `subtask_finished` | Per-subtask lifecycle of an orchestration child run; summaries carry the step key plus parent and child run ids |
| `audit_exported` / `evidence_exported` | Exports are themselves audited |
| `auth_failure` | A JWT-mode authentication failure |

## What is never logged

- No raw secrets, tokens, passwords, or credential values — connector
  config holds references only, and references appear in audit only as
  `<ref>` markers.
- Tool-call inputs are recorded as a **SHA-256 hash plus a redacted
  summary** (secret-shaped substrings are replaced with `[REDACTED]`),
  never as raw payloads.
- Draft contents are hashed into the audit entry, not copied into it; the
  draft itself lives in the run record.

## Viewing, filtering, export

- UI: Audit page with event-type filter, chain verification, and export
  buttons.
- API: `GET /api/audit` (filter by `event_type`, `actor_name`, `agent_id`,
  `connector_id`), `GET /api/audit/verify`,
  `GET /api/audit/export?format=json|csv`.
- **Evidence Pack** per run: `GET /api/runs/{run_id}/evidence` returns one
  JSON document with the run, its approvals/applied writes, its tool
  calls, the reviewer verdict, the audit slice for that run, and a fresh
  chain-verification result — the bundle you hand an auditor.

## Retention

Entries live in the same database as the rest of the state and survive
restarts (covered by the persistence-reopen test). Retention policy is a
deployment decision: export to your SIEM/archive on your schedule — the
export endpoints are the integration point — but note that pruning a
hash-chained log must be done by archiving whole chain segments, never by
editing entries in place.
