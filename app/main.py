"""Open Agent Plane API + single static ops UI."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import gateway, service
from .auth import Caller, get_actor, get_caller, validate_auth_config
from .connectors import get_adapter, load_catalog, validate_config
from .models import Agent, ConnectorInstance, MemoryItem, ModelSettings
from .skills import load_skills
from .store import Store, audit

STATIC = Path(__file__).resolve().parent / "static"


class ModelIn(BaseModel):
    model: str = ""

class ConnectorIn(BaseModel):
    catalog_id: str
    display_name: str = ""
    config: dict[str, str] = Field(default_factory=dict)

class AgentIn(BaseModel):
    name: str
    owner: str
    description: str = ""
    allowed_connectors: list[str] = Field(default_factory=list)
    allowed_skills: list[str] = Field(default_factory=list)
    model_override: Optional[str] = None

class RunIn(BaseModel):
    agent_id: str
    skill: str
    input: str = ""
    connector_id: str = ""
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

class ApprovalIn(BaseModel):
    approver: str = ""
    reason: str = ""

class MemoryIn(BaseModel):
    tier: str
    owner: str = ""
    content: str

class PromoteIn(BaseModel):
    approver: str = ""

class SummarizeIn(BaseModel):
    agent_id: str
    tier: str
    owner: str = ""
    source_item_ids: list[str] = Field(default_factory=list)
    limit: int = 20
    input: str = ""

class SubtaskIn(BaseModel):
    key: str
    agent_id: str = ""  # blank = the lead agent
    skill: str
    input: str = ""
    depends_on: list[str] = Field(default_factory=list)
    connector_id: str = ""
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

class OrchestrateIn(BaseModel):
    lead_agent_id: str
    task: str
    subtasks: Optional[list[SubtaskIn]] = None



def create_app(store: Store | None = None) -> FastAPI:
    validate_auth_config()
    app = FastAPI(title="Open Agent Plane", version="1.3.0")
    app.state.store = store or Store()

    def st(request: Request) -> Store:
        return request.app.state.store

    # ---- RBAC ------------------------------------------------------------
    def deny(request: Request, caller: Caller, action: str):
        """Audit an authorization denial, then raise 403. Never returns."""
        audit(st(request), "authz_denied", actor=caller.actor,
              summary=f"authorization denied: role={caller.role} "
                      f"attempted {action}",
              detail=f"method={request.method} path={request.url.path} "
                     f"actor={caller.actor} role={caller.role}")
        raise HTTPException(
            403, f"role {caller.role} may not perform: {action}")

    def require_roles(request: Request, caller: Caller, action: str,
                      *roles: str) -> None:
        if caller.role not in roles:
            deny(request, caller, action)

    # ---- basics ---------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health(request: Request):
        s = st(request)
        return {"ok": True, "service": "open-agent-plane",
                "model": gateway.effective_model(s) or gateway.TEST_MODEL_LABEL,
                "audit_chain": s.verify_audit_chain()}

    # ---- settings / model ----------------------------------------------
    @app.get("/api/me")
    def me(caller: Caller = Depends(get_caller)):
        from .auth import auth_mode
        return {"actor": caller.actor, "role": caller.role,
                "auth_mode": auth_mode()}

    @app.get("/api/settings")
    def get_settings(request: Request, caller: Caller = Depends(get_caller)):
        s = st(request)
        settings = s.get_settings()
        return {"model": settings.model or gateway.TEST_MODEL_LABEL,
                "placeholder": gateway.is_placeholder(settings.model),
                "actor": caller.actor, "role": caller.role,
                "evals": [e.model_dump(mode="json") for e in s.all_evals()]}


    @app.put("/api/settings/model")
    def set_model(body: ModelIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "set the model", "admin")
        s = st(request)
        actor = caller.actor
        s.put_settings(ModelSettings(model=body.model.strip(), updated_by=actor))
        audit(s, "model_set", actor=actor,
              summary=f"model set to {body.model or gateway.TEST_MODEL_LABEL}",
              detail=body.model)
        return {"model": body.model or gateway.TEST_MODEL_LABEL}

    @app.post("/api/settings/model/evaluate")
    def evaluate(request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "evaluate the model", "admin")
        s = st(request)
        results = gateway.evaluate_model(s, caller.actor)
        return {"results": [r.model_dump(mode="json") for r in results]}

    # ---- connectors ------------------------------------------------------
    @app.get("/api/catalog")
    def catalog(request: Request, actor: str = Depends(get_actor)):
        return [e.model_dump(mode="json") for e in load_catalog().values()]

    @app.get("/api/connectors")
    def list_connectors(request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        cat = load_catalog()
        out = []
        for inst in s.all_connectors():
            entry = cat.get(inst.catalog_id)
            d = inst.model_dump(mode="json")
            d["status"] = entry.status if entry else "UNKNOWN"
            d["name"] = entry.name if entry else inst.catalog_id
            out.append(d)
        return out


    @app.post("/api/connectors", status_code=201)
    def add_connector(body: ConnectorIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "configure a connector", "admin")
        actor = caller.actor
        s = st(request)
        cat = load_catalog()
        entry = cat.get(body.catalog_id)
        if entry is None:
            raise HTTPException(404, f"unknown catalog connector: {body.catalog_id}")
        errors = validate_config(entry, body.config)
        if errors:
            raise HTTPException(422, "; ".join(errors))
        inst = ConnectorInstance(catalog_id=body.catalog_id,
                                 display_name=body.display_name,
                                 config=body.config, created_by=actor)
        s.put_connector(inst)
        audit(s, "connector_added", actor=actor, connector_id=inst.id,
              summary=f"connector configured: {entry.id} ({entry.status})",
              detail=json.dumps({k: ("<ref>" if k.endswith('_ref') else v)
                                 for k, v in body.config.items()}))
        return inst.model_dump(mode="json")

    @app.post("/api/connectors/{instance_id}/test")
    def test_connector(instance_id: str, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "test a connector", "admin")
        actor = caller.actor
        s = st(request)
        inst = s.get_connector(instance_id)
        if inst is None:
            raise HTTPException(404, "connector instance not found")
        entry = load_catalog()[inst.catalog_id]
        result = get_adapter(entry).health(inst.config)
        inst.last_health = result
        s.put_connector(inst)
        audit(s, "connector_tested", actor=actor, connector_id=inst.id,
              summary=f"health check {entry.id}: ok={result.get('ok')} live={result.get('live')}",
              detail=json.dumps(result, default=str))
        return result

    # ---- agents ----------------------------------------------------------
    @app.get("/api/agents")
    def list_agents(request: Request, actor: str = Depends(get_actor)):
        return [a.model_dump(mode="json") for a in st(request).all_agents()]


    @app.post("/api/agents", status_code=201)
    def add_agent(body: AgentIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "create an agent", "admin", "owner")
        if caller.role != "admin" and body.owner != caller.actor:
            deny(request, caller, "create an agent owned by someone else")
        actor = caller.actor
        s = st(request)
        agent = Agent(**body.model_dump(), created_by=actor)
        s.put_agent(agent)
        audit(s, "agent_created", actor=actor, agent_id=agent.id,
              summary=f"agent created: {agent.name} (owner: {agent.owner})",
              detail=agent.model_dump_json())
        return agent.model_dump(mode="json")

    # ---- skills ----------------------------------------------------------
    @app.get("/api/skills")
    def list_skills(request: Request, actor: str = Depends(get_actor)):
        return [{k: v for k, v in sk.model_dump(mode="json").items() if k != "body"}
                for sk in load_skills().values()]

    # ---- runs ------------------------------------------------------------

    @app.post("/api/runs", status_code=201)
    def create_run(body: RunIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "create a run", "admin", "owner", "user")
        s = st(request)
        actor = caller.actor
        if caller.role != "admin":
            agent = s.get_agent(body.agent_id)
            if agent is not None and agent.owner != actor:
                deny(request, caller, "run an agent owned by someone else")
        from .models import Run
        run = Run(_store=s, **body.model_dump(), created_by=actor)
        run.save()
        run = service.execute_run(s, run, actor)
        return run.model_dump(mode="json")

    @app.post("/api/orchestrate", status_code=201)
    def create_orchestration(body: OrchestrateIn, request: Request,
                             caller: Caller = Depends(get_caller)):
        """Create an orchestration: a parent run whose plan executes as
        governed child runs (skill: orchestrate). Subtasks may be
        explicit or omitted (the planner proposes a plan through the
        model gateway; the TestModel placeholder yields a deterministic
        placeholder plan, labelled on the parent run). Children inherit
        governance: each subtask agent must be runnable by the caller
        under the create_run ownership scoping, and child writes park
        the parent in awaiting_approval until a named approver (never
        the orchestration's creator) decides the child."""
        require_roles(request, caller, "create an orchestration",
                      "admin", "owner", "user")
        s = st(request)
        actor = caller.actor
        lead = s.get_agent(body.lead_agent_id)
        if lead is None:
            raise HTTPException(404, "lead agent not found")
        if caller.role != "admin" and lead.owner != actor:
            deny(request, caller, "orchestrate with a lead agent owned "
                                  "by someone else")
        skills = load_skills()
        if body.subtasks is None:
            raw_steps, source, placeholder, label = \
                service.build_auto_plan(s, lead, body.task)
        else:
            raw_steps = [st_.model_dump() for st_ in body.subtasks]
            source = "explicit"
            label_pref = lead.model_override or gateway.effective_model(s)
            placeholder = gateway.is_placeholder(label_pref)
            label = gateway.TEST_MODEL_LABEL if placeholder else label_pref
        if not raw_steps:
            raise HTTPException(422, "orchestration plan has no subtasks")
        # ---- validate the plan (fail loudly) --------------------------
        steps: list[dict] = []
        seen: set[str] = set()
        for raw in raw_steps:
            key = str(raw.get("key") or "").strip()
            if not key:
                raise HTTPException(422, "every subtask needs a key")
            if key in seen:
                raise HTTPException(422, f"duplicate subtask key: {key}")
            seen.add(key)
            agent_id = str(raw.get("agent_id") or lead.id)
            agent = s.get_agent(agent_id)
            if agent is None:
                raise HTTPException(
                    422, f"subtask {key}: unknown agent {agent_id}")
            if caller.role != "admin" and agent.owner != actor:
                deny(request, caller,
                     f"orchestrate subtask {key} with an agent owned "
                     f"by someone else")
            skill = str(raw.get("skill") or "")
            if skill == "orchestrate":
                raise HTTPException(
                    422, f"subtask {key}: nested orchestration is not "
                         f"supported")
            if skill not in skills:
                raise HTTPException(
                    422, f"subtask {key}: unknown skill: {skill}")
            if agent.allowed_skills and skill not in agent.allowed_skills:
                raise HTTPException(
                    422, f"subtask {key}: skill {skill} is not in agent "
                         f"{agent.name}'s allow-list")
            steps.append({
                "key": key, "agent_id": agent_id, "skill": skill,
                "input": str(raw.get("input") or ""),
                "depends_on": [str(d) for d in raw.get("depends_on") or []],
                "connector_id": str(raw.get("connector_id") or ""),
                "action": str(raw.get("action") or ""),
                "params": dict(raw.get("params") or {}),
            })
        for step in steps:
            for dep in step["depends_on"]:
                if dep not in seen:
                    raise HTTPException(
                        422, f"subtask {step['key']}: unknown depends_on "
                             f"key: {dep}")
        # Topological order (stable: plan order wins among ready steps).
        ordered: list[dict] = []
        done: set[str] = set()
        remaining = list(steps)
        while remaining:
            ready = [st_ for st_ in remaining
                     if all(d in done for d in st_["depends_on"])]
            if not ready:
                raise HTTPException(
                    422, "subtask dependency graph has a cycle")
            nxt = ready[0]
            ordered.append(nxt)
            done.add(nxt["key"])
            remaining.remove(nxt)
        from .models import Run
        parent = Run(_store=s, agent_id=lead.id, skill="orchestrate",
                     input=body.task,
                     params={"task": body.task, "plan": ordered,
                             "plan_source": source},
                     created_by=actor, model_used=label,
                     model_placeholder=placeholder)
        parent.save()
        audit(s, "orchestration_started", actor=actor, agent_id=lead.id,
              run_id=parent.id,
              summary=f"orchestration started: {len(ordered)} subtask(s), "
                      f"plan={source}, lead agent {lead.name}"
                      + (" [placeholder plan]" if source == "placeholder"
                         else ""),
              detail=body.task)
        parent = service.advance_orchestration(s, parent)
        return parent.model_dump(mode="json")

    @app.get("/api/runs")
    def list_runs(request: Request, actor: str = Depends(get_actor)):
        runs = sorted(st(request).all_runs(), key=lambda r: r.created_at, reverse=True)
        return [r.model_dump(mode="json") for r in runs]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request, actor: str = Depends(get_actor)):
        run = st(request).get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run.model_dump(mode="json")

    @app.get("/api/approvals")
    def approvals(request: Request, actor: str = Depends(get_actor)):
        runs = [r for r in st(request).all_runs() if r.status == "awaiting_approval"]
        return [r.model_dump(mode="json") for r in runs]


    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: ApprovalIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "approve a run", "approver", "admin")
        s = st(request)
        run = s.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.skill == "orchestrate":
            raise HTTPException(
                409, "orchestration runs are not approved directly; "
                     "approve the awaiting child run instead (see the "
                     "parent run's draft for the child run id)")
        if run.status != "awaiting_approval":
            raise HTTPException(409, f"run is {run.status}, not awaiting approval")
        approver = body.approver or caller.actor
        if approver == run.created_by:
            deny(request, caller,
                 "approve a run they created (separation of duties)")
        updated = service.approve_run(s, run, approver)
        if run.parent_run_id:
            parent = s.get_run(run.parent_run_id)
            if parent is not None and parent.skill == "orchestrate":
                service.advance_orchestration(s, parent)
        return updated.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, body: ApprovalIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "reject a run", "approver", "admin")
        s = st(request)
        run = s.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.skill == "orchestrate":
            raise HTTPException(
                409, "orchestration runs are not rejected directly; "
                     "reject the awaiting child run instead (see the "
                     "parent run's draft for the child run id)")
        if run.status != "awaiting_approval":
            raise HTTPException(409, f"run is {run.status}, not awaiting approval")
        approver = body.approver or caller.actor
        if approver == run.created_by:
            deny(request, caller,
                 "reject a run they created (separation of duties)")
        updated = service.reject_run(s, run, approver, body.reason)
        if run.parent_run_id:
            parent = s.get_run(run.parent_run_id)
            if parent is not None and parent.skill == "orchestrate":
                service.advance_orchestration(s, parent,
                                               fail_reason=body.reason)
        return updated.model_dump(mode="json")

    # ---- memory ------------------------------------------------------------

    @app.get("/api/memory")
    def list_memory(request: Request, tier: str = "", actor: str = Depends(get_actor)):
        items = st(request).all_memory()
        if tier:
            items = [m for m in items if m.tier == tier]
        return [m.model_dump(mode="json") for m in items]

    @app.post("/api/memory", status_code=201)
    def add_memory(body: MemoryIn, request: Request, caller: Caller = Depends(get_caller)):
        require_roles(request, caller, "add memory",
                      "admin", "owner", "user", "approver")
        actor = caller.actor
        s = st(request)
        if body.tier not in ("private", "team", "org"):
            raise HTTPException(422, "tier must be private|team|org")
        item = MemoryItem(tier=body.tier, owner=body.owner or actor,
                          content=body.content, created_by=actor)
        s.put_memory(item)
        audit(s, "memory_added", actor=actor,
              summary=f"memory added to {body.tier} tier", detail=body.content)
        return item.model_dump(mode="json")


    @app.post("/api/memory/summarize", status_code=201)
    def summarize_memory(body: SummarizeIn, request: Request,
                         caller: Caller = Depends(get_caller)):
        """Create a governed memory-summary RUN (skill: memory-summary).

        The summary is a WRITE: the run drafts it, the reviewer can veto,
        and a named human must approve (never the run's creator) before
        a kind="summary" memory item is stored. Originals never change.
        Scope gates: private = the caller's own items (admin: any
        owner); org drafting is admin-only; auditors may never draft.
        """
        require_roles(request, caller, "draft a memory summary",
                      "admin", "owner", "user")
        s = st(request)
        actor = caller.actor
        tier = body.tier.strip().lower()
        if tier not in ("private", "team", "org"):
            raise HTTPException(422, "tier must be private|team|org")
        if tier == "org":
            require_roles(request, caller, "draft an org memory summary",
                          "admin")
            owner = body.owner.strip() or "org"
        elif tier == "private":
            owner = body.owner.strip() or actor
            if caller.role != "admin" and owner != actor:
                deny(request, caller,
                     "summarize another user's private memory")
        else:  # team
            owner = body.owner.strip()
            if not owner:
                raise HTTPException(
                    422, "team summaries require an owner (team name)")
        agent = s.get_agent(body.agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        if caller.role != "admin" and agent.owner != actor:
            deny(request, caller, "run an agent owned by someone else")
        # Source selection: explicit ids (validated against the scope)
        # or the latest N *fact* items of the scope (summaries are never
        # silently folded into new summaries).
        if body.source_item_ids:
            items = []
            for sid in body.source_item_ids:
                item = s.get_memory(sid)
                if item is None:
                    raise HTTPException(422, f"unknown memory item: {sid}")
                if item.tier != tier or item.owner != owner:
                    deny(request, caller,
                         "summarize memory items outside the requested scope")
                items.append(item)
        else:
            scoped = [m for m in s.all_memory()
                      if m.tier == tier and m.owner == owner
                      and m.kind == "fact"]
            scoped.sort(key=lambda m: (m.created_at, m.id))
            limit = max(1, min(body.limit or 20, 100))
            items = scoped[-limit:]
        if not items:
            raise HTTPException(
                422, f"no memory items in scope {tier}/{owner} to summarize")
        source_ids = [m.id for m in items]
        from .models import Run
        run = Run(_store=s, agent_id=agent.id, skill="memory-summary",
                  input=body.input or f"Summarize {tier} memory for {owner}",
                  params={"summary_tier": tier, "summary_owner": owner,
                          "source_ids": source_ids,
                          "source_count": len(source_ids)},
                  created_by=actor)
        run.save()
        audit(s, "memory_summary_requested", actor=actor, agent_id=agent.id,
              run_id=run.id,
              summary=f"memory summary requested: {tier}/{owner} from "
                      f"{len(source_ids)} source(s) via agent {agent.name}",
              detail=", ".join(source_ids))
        run = service.execute_run(s, run, actor)
        return run.model_dump(mode="json")

    @app.post("/api/memory/{item_id}/promote")
    def promote_memory(item_id: str, body: PromoteIn, request: Request,
                       caller: Caller = Depends(get_caller)):
        """Promotion private -> team is human-approved only, and audited."""
        require_roles(request, caller, "promote memory", "approver", "admin")
        actor = caller.actor
        s = st(request)
        item = s.get_memory(item_id)
        if item is None:
            raise HTTPException(404, "memory item not found")
        approver = body.approver or actor
        if approver == item.owner:
            deny(request, caller,
                 "promote their own memory item (separation of duties)")
        promoted = MemoryItem(tier="team", owner=item.owner, content=item.content,
                              created_by=actor, promoted_from=item.id)
        s.put_memory(promoted)
        audit(s, "memory_promoted", actor=approver,
              summary=f"memory {item.id} promoted private->team by {approver}",
              detail=item.content)
        return promoted.model_dump(mode="json")

    # ---- audit -------------------------------------------------------------
    @app.get("/api/audit")
    def list_audit(request: Request, actor: str = Depends(get_actor),
                   event_type: str = "", actor_filter: str = Query("", alias="actor_name"),
                   agent_id: str = "", connector_id: str = ""):
        entries = st(request).all_audit()
        if event_type:
            entries = [e for e in entries if e.event_type == event_type]
        if actor_filter:
            entries = [e for e in entries if e.actor == actor_filter]
        if agent_id:
            entries = [e for e in entries if e.agent_id == agent_id]
        if connector_id:
            entries = [e for e in entries if e.connector_id == connector_id]
        return [e.model_dump(mode="json") for e in entries]

    @app.get("/api/audit/verify")
    def verify_audit(request: Request, actor: str = Depends(get_actor)):
        return st(request).verify_audit_chain()

    @app.get("/api/audit/export")
    def export_audit(request: Request, format: str = "json", actor: str = Depends(get_actor)):
        s = st(request)
        entries = s.all_audit()
        audit(s, "audit_exported", actor=actor,
              summary=f"audit exported as {format} ({len(entries)} entries)")
        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["seq", "at", "event_type", "actor", "agent_id",
                             "connector_id", "run_id", "summary", "entry_hash"])
            for e in entries:
                writer.writerow([e.seq, e.at.isoformat(), e.event_type, e.actor,
                                 e.agent_id, e.connector_id, e.run_id, e.summary,
                                 e.entry_hash])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv")
        return JSONResponse([e.model_dump(mode="json") for e in entries])

    @app.get("/api/runs/{run_id}/evidence")
    def evidence_pack(run_id: str, request: Request, actor: str = Depends(get_actor)):
        """Per-run Evidence Pack: run + approvals + tool calls + the audit
        slice for the run + a fresh chain-verification result."""
        s = st(request)
        run = s.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        children = [r for r in s.all_runs() if r.parent_run_id == run_id]
        child_ids = {c.id for c in children}
        audit_slice = [e.model_dump(mode="json") for e in s.all_audit()
                       if e.run_id == run_id or e.run_id in child_ids]
        pack = {
            "run": run.model_dump(mode="json"),
            "child_runs": [c.model_dump(mode="json") for c in children],
            "approvals": [a.model_dump(mode="json") for a in s.all_applied()
                          if a.run_id == run_id or a.run_id in child_ids],
            "tool_calls": [tc.model_dump(mode="json") for tc in run.tool_calls],
            "review": run.review.model_dump(mode="json") if run.review else None,
            "audit_slice": audit_slice,
            "chain_verification": s.verify_audit_chain(),
            "exported_by": actor,
        }
        audit(s, "evidence_exported", actor=actor, run_id=run_id,
              summary=f"evidence pack exported for run {run_id}")
        return JSONResponse(pack)

    return app


app = None  # created by uvicorn factory below


def build_app() -> FastAPI:
    return create_app(Store(os.environ.get("AGENTPLANE_DB", "data/agentplane.db")))
