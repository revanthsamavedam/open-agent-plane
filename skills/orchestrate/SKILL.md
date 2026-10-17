---
name: orchestrate
version: 1.0.0
description: Meta-skill — execute a plan of subtasks as governed child runs (Muse-style delegation) with typed handoffs, inherited RBAC and approval gates.
write: false
---
# Orchestrate

1. Orchestrations are created **only** via `POST /api/orchestrate`
   with a lead agent and a task. A generic run with skill
   `orchestrate` fails closed. Subtasks may be given explicitly
   (`key`, `agent_id`, `skill`, `input`, `depends_on`) or omitted —
   then the planner proposes a plan through the model gateway; with
   the offline TestModel the plan is a deterministic placeholder
   (a `data-lookup` step feeding a `draft-report` step) and the
   parent run is labelled placeholder.
2. The plan is a DAG: keys are unique, `depends_on` references must
   exist, and cycles are rejected (422). Every subtask's skill must
   be in its agent's allow-list, and every subtask agent must be
   runnable by the caller under the same ownership scoping as
   ordinary runs (violations are 403, audited as `authz_denied`).
3. Execution is sequential in topological order. Each subtask runs
   as a first-class **child run** through the ordinary pipeline —
   certification gate, connector calls, reviewer verdict — carrying
   `parent_run_id` and `plan_step`. A child's input may embed
   `{{key.result}}` templates, resolved from that dependency's
   result at execution time; an unresolvable template fails the
   child (and the parent) — never a silent substitution.
4. Governance is inherited: if a child reaches `awaiting_approval`
   (a write skill), the parent parks in `awaiting_approval` naming
   the waiting child. Approval/rejection happens on the **child**
   via the ordinary endpoints — the orchestration's creator can
   never approve its children (separation of duties). Child approval
   resumes the parent; child rejection or veto fails it with the
   reason recorded.
5. When all children complete, the parent's result is a synthesis
   of the child result summaries (a deterministic digest by
   default; a real configured model may write the synthesis,
   labelled with the model). Started / finished / failed are
   audited per subtask and per orchestration, and the parent's
   Evidence Pack includes the child runs.
