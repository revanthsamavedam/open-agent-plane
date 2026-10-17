"""Run execution: governed, reviewer-gated, approval-gated, audited.

Flow: run -> (optional connector read = tool call, audited) -> draft ->
reviewer verdict (veto stops the run) -> awaiting named-human approval ->
approve applies the draft via the connector adapter (LIVE writes land in
the stand-in; SCAFFOLD writes are recorded intent, labelled) / reject ends
the run. Read-only skills finish without approval. Everything is audited.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import gateway
from .connectors import get_adapter, load_catalog, summarize_result
from .models import (
    AppliedWrite, MemoryItem, ReviewVerdict, Run, ToolCall, redact,
    sha256_text,
)
from .skills import load_skills
from .store import Store, audit


def review_draft(run: Run, draft: str) -> ReviewVerdict:
    reasons: list[str] = []
    ok = True
    if not draft.strip():
        ok = False
        reasons.append("draft is empty")
    from .models import looks_like_raw_secret

    if looks_like_raw_secret(draft):
        ok = False
        reasons.append("draft appears to contain a raw secret")
    if len(draft) > 20000:
        ok = False
        reasons.append("draft exceeds size limit")
    if ok:
        reasons.append("non-empty, no secret patterns, within size limits")
    return ReviewVerdict(approved=ok, reasons=reasons)


SUMMARY_SKILL = "memory-summary"
ORCHESTRATE_SKILL = "orchestrate"


def extractive_digest(items: list[MemoryItem], tier: str, owner: str) -> str:
    """Deterministic digest used when the model is the offline TestModel
    placeholder (and as a fallback if a real-model call fails): the first
    sentence of each source item, in source order, each tagged with the
    source item id so provenance is visible in the draft itself."""
    lines = [f"Digest of {len(items)} {tier} memory item(s) owned by {owner}:"]
    for item in items:
        text = " ".join(item.content.split())
        first = text.split(". ")[0].strip()
        if first and not first.endswith("."):
            first += "."
        lines.append(f"- [{item.id}] {first[:200]}")
    return "\n".join(lines)


def build_summary_draft(store: Store, run: Run, agent_name: str,
                        agent_owner: str, label: str,
                        placeholder: bool) -> str:
    """Compose the memory-summary draft from its governed scope. Sources
    were selected and validated by POST /api/memory/summarize; they are
    re-loaded here and re-checked defensively (fail closed)."""
    tier = str(run.params.get("summary_tier") or "")
    owner = str(run.params.get("summary_owner") or "")
    source_ids = [str(x) for x in (run.params.get("source_ids") or [])]
    if run.skill != SUMMARY_SKILL or not tier or not source_ids:
        raise ValueError(
            "memory-summary runs must be created via POST /api/memory/summarize "
            "with a tier + owner scope and selected sources")
    items: list[MemoryItem] = []
    for sid in source_ids:
        item = store.get_memory(sid)
        if item is None or item.tier != tier or item.owner != owner:
            raise ValueError(f"summary source {sid} is outside scope {tier}/{owner}")
        items.append(item)
    if placeholder:
        digest = extractive_digest(items, tier, owner)
    else:
        joined = "\n".join(f"[{it.id}] {it.content}" for it in items)
        try:
            digest, _, _ = gateway.generate(
                store,
                "Summarize the following memory items into a concise digest "
                "of bullet points. Preserve names, decisions and facts; do "
                f"not invent anything.\n\n{joined}")
            if not digest.strip():
                digest = extractive_digest(items, tier, owner)
        except Exception:
            digest = extractive_digest(items, tier, owner)
    lines = [f"# memory-summary — {tier} memory for {owner}",
             "",
             f"Agent: {agent_name} (owner: {agent_owner})",
             f"Model: {label}" + (" [placeholder]" if placeholder else ""),
             "",
             digest,
             "",
             f"Sources ({len(items)}): " + ", ".join(it.id for it in items)]
    return "\n".join(lines)


def execute_run(store: Store, run: Run, actor: str) -> Run:
    catalog = load_catalog()
    skills = load_skills()
    run._store = store
    audit(store, "run_started", actor=actor, agent_id=run.agent_id, run_id=run.id,
          connector_id=run.connector_id,
          summary=f"run started: skill={run.skill}", detail=run.input)
    try:
        agent = store.get_agent(run.agent_id)
        if agent is None:
            raise ValueError(f"unknown agent: {run.agent_id}")
        if run.skill not in skills:
            raise ValueError(f"unknown skill: {run.skill}")
        skill = skills[run.skill]
        if agent.allowed_skills and run.skill not in agent.allowed_skills:
            raise ValueError(f"agent {agent.name} is not allowed skill {run.skill}")
        if run.skill == ORCHESTRATE_SKILL:
            raise ValueError(
                "skill 'orchestrate' is a meta-skill: orchestration runs "
                "must be created via POST /api/orchestrate")

        model_label_pref = agent.model_override or gateway.effective_model(store)
        placeholder = gateway.is_placeholder(model_label_pref)
        label = gateway.TEST_MODEL_LABEL if placeholder else model_label_pref

        tool_result: dict | None = None
        entry = None
        adapter = None
        if run.connector_id:
            instance = store.get_connector(run.connector_id)
            if instance is None:
                raise ValueError(f"unknown connector instance: {run.connector_id}")
            if agent.allowed_connectors and run.connector_id not in agent.allowed_connectors:
                raise ValueError(f"agent {agent.name} is not allowed connector {run.connector_id}")
            entry = catalog[instance.catalog_id]
            adapter = get_adapter(entry)
            action = run.action or (entry.actions[0] if entry.actions else "read")
            params = dict(run.params or {})
            tool_result = adapter.read(instance.config, action, params)
            tc = ToolCall(
                connector_id=instance.id, catalog_id=entry.id, action=action,
                input_hash=sha256_text(json.dumps(params, sort_keys=True, default=str)),
                input_summary=redact(json.dumps(params, default=str)),
                result_summary=summarize_result(tool_result),
                scaffold=not adapter.live,
            )
            run.tool_calls.append(tc)
            audit(store, "tool_call", actor=actor, agent_id=run.agent_id,
                  connector_id=instance.id, run_id=run.id,
                  summary=f"{entry.id}.{action}: {tc.result_summary}",
                  detail=json.dumps(params, default=str))

        # Model certification gate (skipped in placeholder/demo mode).
        allowed, gate_note = gateway.model_gate(store, label, placeholder, run.skill)
        if not allowed:
            raise ValueError(gate_note)

        # Build the work product. The configured model is consulted for a
        # one-line summary; the draft itself is composed deterministically
        # from the tool results so runs are reproducible and reviewable.
        facts = ""
        if tool_result is not None:
            rows = tool_result.get("rows") or tool_result.get("objects") or []
            facts = json.dumps(rows, default=str)[:1500]
        try:
            model_note, _, _ = gateway.generate(
                store, f"Summarize in one line for skill {run.skill}: {run.input}"
            )
        except Exception:
            model_note = ""  # model is advisory here; the draft stands on facts
        run.model_used = label
        run.model_placeholder = placeholder

        # memory-summary: the draft is a governed digest of the selected
        # memory sources (validated scope lives in run.params).
        summary_draft = None
        if run.skill == SUMMARY_SKILL:
            summary_draft = build_summary_draft(
                store, run, agent.name, agent.owner, label, placeholder)

        if skill.write:
            if summary_draft is not None:
                run.draft = summary_draft
            else:
                lines = [f"# {run.skill} — {run.input}".strip(),
                         "", f"Agent: {agent.name} (owner: {agent.owner})",
                         f"Model: {label}" + (" [placeholder]" if placeholder else "")]
                if facts:
                    lines += ["", "Facts from connector read:", facts]
                if model_note and not placeholder:
                    lines += ["", f"Model summary: {model_note}"]
                lines += ["", f"Request: {run.input}"]
                run.draft = "\n".join(lines)
            verdict = review_draft(run, run.draft)
            run.review = verdict
            audit(store, "reviewer_verdict", actor="reviewer-agent",
                  agent_id=run.agent_id, run_id=run.id,
                  summary=f"reviewer {'approved' if verdict.approved else 'VETOED'}: "
                          + "; ".join(verdict.reasons),
                  detail=run.draft)
            if not verdict.approved:
                run.status = "vetoed"
            else:
                run.status = "awaiting_approval"
            if summary_draft is not None:
                audit(store, "memory_summary_drafted", actor=actor,
                      agent_id=run.agent_id, run_id=run.id,
                      summary=f"memory summary drafted: "
                              f"{run.params.get('summary_tier')}/"
                              f"{run.params.get('summary_owner')} from "
                              f"{len(run.params.get('source_ids') or [])} source(s); "
                              f"run {run.status}",
                      detail=run.draft)
        else:
            run.draft = facts or (tool_result.get("content", "") if tool_result else "") \
                or f"No connector read; nothing to look up for: {run.input}"
            run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        run.save()
        audit(store, "run_finished", actor=actor, agent_id=run.agent_id,
              run_id=run.id, connector_id=run.connector_id,
              summary=f"run {run.status}: skill={run.skill} model={label}",
              detail=run.draft)
        return run
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:500]
        run.finished_at = datetime.now(timezone.utc)
        run.save()
        audit(store, "run_failed", actor=actor, agent_id=run.agent_id,
              run_id=run.id, connector_id=run.connector_id,
              summary=f"run failed: {run.error}", detail=run.input)
        return run


def approve_run(store: Store, run: Run, approver: str) -> Run:
    catalog = load_catalog()
    run._store = store
    applied_note = "no connector: draft approved and recorded as applied in the run"
    live = False
    catalog_id = ""
    summary_item: MemoryItem | None = None
    if run.skill == SUMMARY_SKILL and not run.connector_id:
        # The apply of a memory summary stores a NEW derived memory item.
        # Sources are never modified; rejection/veto never reaches here.
        tier = str(run.params.get("summary_tier") or "")
        owner = str(run.params.get("summary_owner") or "")
        source_ids = [str(x) for x in (run.params.get("source_ids") or [])]
        summary_item = MemoryItem(
            tier=tier, owner=owner, content=run.draft, kind="summary",
            source_ids=source_ids, generated_by_run_id=run.id,
            model=run.model_used, created_by=run.created_by)
        store.put_memory(summary_item)
        applied_note = (f"memory summary stored as {summary_item.id} "
                        f"({len(source_ids)} source(s), {tier}/{owner}); "
                        "sources unchanged")
    if run.connector_id:
        instance = store.get_connector(run.connector_id)
        if instance is not None:
            catalog_id = instance.catalog_id
            entry = catalog[instance.catalog_id]
            adapter = get_adapter(entry)
            result = adapter.write_draft(instance.config, run.draft)
            live = bool(result.get("live"))
            applied_note = result.get("detail", "") + (
                "" if live else " (SCAFFOLD: recorded intent, no vendor write performed)"
            )
    store.put_applied(AppliedWrite(run_id=run.id, connector_id=run.connector_id,
                                   catalog_id=catalog_id,
                                   live=live, note=applied_note, approved_by=approver))
    run.status = "applied"
    run.approver = approver
    run.applied_note = applied_note
    run.save()
    audit(store, "approval", actor=approver, agent_id=run.agent_id, run_id=run.id,
          connector_id=run.connector_id,
          summary=f"approved by {approver}; applied: {applied_note}", detail=run.draft)
    if summary_item is not None:
        audit(store, "memory_summary_applied", actor=approver,
              agent_id=run.agent_id, run_id=run.id,
              summary=f"memory summary applied: {summary_item.id} stored for "
                      f"{summary_item.tier}/{summary_item.owner} from "
                      f"{len(summary_item.source_ids)} source(s) "
                      f"(model: {summary_item.model or 'unknown'})",
              detail=run.draft)
    return run


def reject_run(store: Store, run: Run, approver: str, reason: str = "") -> Run:
    run._store = store
    run.status = "rejected"
    run.approver = approver
    run.save()
    audit(store, "rejection", actor=approver, agent_id=run.agent_id, run_id=run.id,
          connector_id=run.connector_id,
          summary=f"rejected by {approver}" + (f": {reason}" if reason else ""),
          detail=run.draft)
    return run


# ---------------------------------------------------------------------------
# Orchestration (v1.3): a run can be a PLAN executed by child runs.
#
# The parent run (skill="orchestrate") holds the plan in params["plan"]:
# a list of steps {key, agent_id, skill, input, depends_on, connector_id,
# action, params} in topological order (validated by the endpoint).
# Each step executes as a first-class child Run through execute_run —
# the pipeline is reused, never forked — carrying parent_run_id +
# plan_step, created_by = the orchestration's creator (so separation of
# duties on child approvals is inherited unchanged).
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.result\s*\}\}")


def resolve_templates(text: str, depends_on: list[str],
                      results: dict[str, str]) -> tuple[str, list[str]]:
    """Resolve {{key.result}} templates from declared dependencies'
    results. Returns (resolved_text, missing_keys)."""
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key in depends_on and key in results:
            return results[key][:1500]
        if key not in missing:
            missing.append(key)
        return match.group(0)

    return _TEMPLATE_RE.sub(repl, text or ""), missing


def deterministic_plan(lead, task: str) -> list[dict]:
    """The placeholder planner's plan: a read feeding a report, using
    the lead agent's allowed skills. Deterministic — same lead + task
    always yields the same plan."""
    allowed = set(lead.allowed_skills) if lead.allowed_skills else None

    def has(skill: str) -> bool:
        return allowed is None or skill in allowed

    steps: list[dict] = []
    if has("data-lookup"):
        steps.append({"key": "lookup", "agent_id": lead.id,
                      "skill": "data-lookup", "input": task,
                      "depends_on": [], "connector_id": "",
                      "action": "", "params": {}})
    if has("draft-report"):
        depends = ["lookup"] if steps else []
        text = f"Write a report for the task: {task}"
        if depends:
            text += "\n\nFindings from the lookup step:\n{{lookup.result}}"
        steps.append({"key": "report", "agent_id": lead.id,
                      "skill": "draft-report", "input": text,
                      "depends_on": depends, "connector_id": "",
                      "action": "", "params": {}})
    if not steps:
        skill = next((s for s in (lead.allowed_skills or [])
                      if s not in (ORCHESTRATE_SKILL, SUMMARY_SKILL)),
                     "daily-brief")
        steps.append({"key": "step1", "agent_id": lead.id, "skill": skill,
                      "input": task, "depends_on": [], "connector_id": "",
                      "action": "", "params": {}})
    return steps


def build_auto_plan(store: Store, lead, task: str):
    """Planner: propose a plan through the model gateway.

    Returns (steps, plan_source, placeholder, model_label). With the
    offline TestModel the plan is the deterministic placeholder plan,
    labelled as such — never presented as a real model's plan. With a
    real model the model is asked for a JSON plan restricted to the
    lead agent and its allowed skills; an unparsable or invalid model
    plan falls back to the deterministic plan (source recorded)."""
    label_pref = lead.model_override or gateway.effective_model(store)
    placeholder = gateway.is_placeholder(label_pref)
    label = gateway.TEST_MODEL_LABEL if placeholder else label_pref
    if placeholder:
        return deterministic_plan(lead, task), "placeholder", True, label
    prompt = (
        "Plan the task below as a small DAG of subtasks for a single "
        "agent. Reply with ONLY a JSON array of objects with keys: "
        "key (short unique string), skill (one of: "
        + ", ".join(sorted(s for s in load_skills()
                           if s != ORCHESTRATE_SKILL))
        + "), input (string; may embed {{key.result}} for a dependency), "
        "depends_on (array of keys).\n\nTask: " + task)
    try:
        text, _, _ = gateway.generate(store, prompt)
        raw = text[text.index("["):text.rindex("]") + 1]
        parsed = json.loads(raw)
        steps = []
        for entry in parsed:
            skill = str(entry["skill"])
            if lead.allowed_skills and skill not in lead.allowed_skills:
                raise ValueError(f"model proposed disallowed skill {skill}")
            steps.append({
                "key": str(entry["key"]), "agent_id": lead.id,
                "skill": skill, "input": str(entry.get("input") or task),
                "depends_on": [str(d) for d in entry.get("depends_on") or []],
                "connector_id": "", "action": "", "params": {}})
        if not steps:
            raise ValueError("model proposed an empty plan")
        return steps, "model", False, label
    except Exception:
        return (deterministic_plan(lead, task), "deterministic-fallback",
                False, label)


def _child_result_text(child: Run) -> str:
    return (child.draft or "").strip()


def synthesize_orchestration(store: Store, parent: Run, plan: list[dict],
                             children: dict[str, Run]) -> str:
    """Parent result: a synthesis of child result summaries. Default is
    a deterministic digest; with a real (non-placeholder) model the
    model writes the synthesis and it is labelled with the model."""
    lines = [f"Orchestration synthesis — task: {parent.input}",
             f"Plan: {len(plan)} subtask(s) "
             f"({parent.params.get('plan_source', 'explicit')}) · "
             f"Model: {parent.model_used}"
             + (" [placeholder]" if parent.model_placeholder else ""),
             ""]
    for step in plan:
        child = children.get(step["key"])
        status = child.status if child else "not run"
        result = _child_result_text(child) if child else ""
        lines.append(f"## [{step['key']}] {step['skill']} — {status}")
        lines.append(result[:800] if result else "(no result)")
        lines.append("")
    digest = "\n".join(lines).strip()
    if not parent.model_placeholder:
        try:
            text, _, _ = gateway.generate(
                store,
                "Synthesize the subtask results below into a concise "
                "final answer for the task. Do not invent facts.\n\n"
                f"Task: {parent.input}\n\n{digest}")
            if text.strip():
                return (f"[synthesis by model {parent.model_used}]\n\n"
                        f"{text.strip()}")
        except Exception:
            pass  # fall through to the deterministic digest
    return digest


def advance_orchestration(store: Store, parent: Run,
                          fail_reason: str = "") -> Run:
    """Execute / resume an orchestration parent run.

    Walks the plan in topological order. Steps without a child run yet
    are executed now (templates resolved from their dependencies);
    steps whose child is awaiting approval park the parent; a child
    in a terminal bad state fails the parent; when every child has
    completed/applied the parent completes with a synthesis. Safe to
    call again after a child approval (resume) or rejection."""
    actor = parent.created_by
    parent._store = store
    plan: list[dict] = list(parent.params.get("plan") or [])
    children: dict[str, Run] = {}
    for run in store.all_runs():
        if run.parent_run_id == parent.id and run.plan_step:
            children[run.plan_step] = run
    results: dict[str, str] = {}

    def fail_parent(error: str) -> Run:
        parent.status = "failed"
        parent.error = error[:500]
        parent.finished_at = datetime.now(timezone.utc)
        parent.save()
        audit(store, "orchestration_failed", actor=actor,
              agent_id=parent.agent_id, run_id=parent.id,
              summary=f"orchestration failed: {error}", detail=error)
        return parent

    for step in plan:
        key = step["key"]
        child = children.get(key)
        if child is None:
            depends = [str(d) for d in step.get("depends_on") or []]
            resolved, missing = resolve_templates(
                str(step.get("input") or ""), depends, results)
            child = Run(
                _store=store,
                agent_id=str(step.get("agent_id") or parent.agent_id),
                skill=str(step.get("skill") or ""),
                input=resolved,
                connector_id=str(step.get("connector_id") or ""),
                action=str(step.get("action") or ""),
                params=dict(step.get("params") or {}),
                parent_run_id=parent.id, plan_step=key,
                created_by=actor)
            child.save()
            children[key] = child
            audit(store, "subtask_started", actor=actor,
                  agent_id=child.agent_id, run_id=child.id,
                  summary=f"subtask started: step={key} "
                          f"skill={child.skill} parent={parent.id} "
                          f"child={child.id}",
                  detail=str(step.get("input") or ""))
            if missing:
                child.status = "failed"
                child.error = (
                    "unresolvable template reference(s): "
                    + ", ".join("{{" + k + ".result}}" for k in missing)
                    + " — templates may only reference declared "
                      "dependencies that have completed")
                child.finished_at = datetime.now(timezone.utc)
                child.save()
                audit(store, "subtask_finished", actor=actor,
                      agent_id=child.agent_id, run_id=child.id,
                      summary=f"subtask finished: step={key} "
                              f"status=failed parent={parent.id} "
                              f"child={child.id}",
                      detail=child.error)
                return fail_parent(f"subtask '{key}' failed: {child.error}")
            child = execute_run(store, child, actor)
            children[key] = child
            audit(store, "subtask_finished", actor=actor,
                  agent_id=child.agent_id, run_id=child.id,
                  summary=f"subtask finished: step={key} "
                          f"status={child.status} parent={parent.id} "
                          f"child={child.id}",
                  detail=child.draft or child.error)
        if child.status in ("completed", "applied"):
            results[key] = _child_result_text(child)
            continue
        if child.status == "awaiting_approval":
            parent.status = "awaiting_approval"
            parent.error = ""
            parent.draft = (
                f"Awaiting approval: child run {child.id} "
                f"(step '{key}', skill {child.skill}) must be approved "
                f"or rejected before the orchestration continues.")
            parent.finished_at = None
            parent.save()
            return parent
        # rejected / vetoed / failed — the parent fails, reason recorded.
        if child.status == "rejected":
            reason = fail_reason or (
                f"rejected by {child.approver}" if child.approver
                else "rejected")
        elif child.status == "vetoed" and child.review is not None:
            reason = "reviewer veto: " + "; ".join(child.review.reasons)
        else:
            reason = child.error or child.status
        return fail_parent(f"subtask '{key}' {child.status}: {reason}")

    parent.draft = synthesize_orchestration(store, parent, plan, children)
    parent.status = "completed"
    parent.error = ""
    parent.finished_at = datetime.now(timezone.utc)
    parent.save()
    audit(store, "orchestration_finished", actor=actor,
          agent_id=parent.agent_id, run_id=parent.id,
          summary=f"orchestration completed: {len(plan)} subtask(s) "
                  f"synthesized",
          detail=parent.draft)
    return parent
