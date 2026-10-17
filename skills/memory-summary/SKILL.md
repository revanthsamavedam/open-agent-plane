---
name: memory-summary
version: 1.0.0
description: Draft a governed summary (derived view) over memory items in one tier + owner scope, with full provenance to the source items.
write: true
---
# Memory Summary

1. Runs are created via `POST /api/memory/summarize` with a scope:
   a tier (private / team / org) plus an owner, and either explicit
   source item ids or a limit (latest N **facts** in the scope —
   summaries are never silently re-summarized). Every source is
   validated against the requested scope; cross-scope sources are
   denied (403, audited as `authz_denied`).
2. The draft is generated from the source contents: with a certified
   real model, the model writes the digest; with the offline TestModel
   placeholder, a deterministic extractive digest is produced and the
   run is labelled placeholder. Scope gates: private summaries cover
   the caller's own items (admins may target any owner); org summaries
   are admin-only to draft; auditors may never draft.
3. The draft goes through the reviewer and requires named human
   approval (the run's creator can never approve — separation of
   duties). Only on approval is a new memory item stored with
   `kind="summary"`, the same tier + owner, `source_ids` pointing at
   every source item, `generated_by_run_id`, and the model label.
   Rejection or a reviewer veto stores nothing. **The summary is a
   derived view: the source items are never modified or deleted.**
