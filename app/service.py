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
