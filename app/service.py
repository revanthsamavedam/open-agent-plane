"""Run execution: governed, reviewer-gated, approval-gated, audited.

Flow: run -> (optional connector read = tool call, audited) -> draft ->
reviewer verdict (veto stops the run) -> awaiting named-human approval ->
approve applies the draft via the connector adapter (LIVE writes land in
the stand-in; SCAFFOLD writes are recorded intent, labelled) / reject ends
the run. Read-only skills finish without approval. Everything is audited.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import gateway
from .connectors import get_adapter, load_catalog, summarize_result
from .models import (
    AppliedWrite, ReviewVerdict, Run, ToolCall, redact, sha256_text,
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

        if skill.write:
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
