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
from .auth import get_actor, validate_auth_config
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



def create_app(store: Store | None = None) -> FastAPI:
    validate_auth_config()
    app = FastAPI(title="Open Agent Plane", version="0.1.0")
    app.state.store = store or Store()

    def st(request: Request) -> Store:
        return request.app.state.store

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
    @app.get("/api/settings")
    def get_settings(request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        settings = s.get_settings()
        return {"model": settings.model or gateway.TEST_MODEL_LABEL,
                "placeholder": gateway.is_placeholder(settings.model),
                "evals": [e.model_dump(mode="json") for e in s.all_evals()]}


    @app.put("/api/settings/model")
    def set_model(body: ModelIn, request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        s.put_settings(ModelSettings(model=body.model.strip(), updated_by=actor))
        audit(s, "model_set", actor=actor,
              summary=f"model set to {body.model or gateway.TEST_MODEL_LABEL}",
              detail=body.model)
        return {"model": body.model or gateway.TEST_MODEL_LABEL}

    @app.post("/api/settings/model/evaluate")
    def evaluate(request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        results = gateway.evaluate_model(s, actor)
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
    def add_connector(body: ConnectorIn, request: Request, actor: str = Depends(get_actor)):
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
    def test_connector(instance_id: str, request: Request, actor: str = Depends(get_actor)):
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
    def add_agent(body: AgentIn, request: Request, actor: str = Depends(get_actor)):
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
    def create_run(body: RunIn, request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        from .models import Run
        run = Run(_store=s, **body.model_dump(), created_by=actor)
        run.save()
        run = service.execute_run(s, run, actor)
        return run.model_dump(mode="json")

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
    def approve(run_id: str, body: ApprovalIn, request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        run = s.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status != "awaiting_approval":
            raise HTTPException(409, f"run is {run.status}, not awaiting approval")
        approver = body.approver or actor
        return service.approve_run(s, run, approver).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, body: ApprovalIn, request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        run = s.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status != "awaiting_approval":
            raise HTTPException(409, f"run is {run.status}, not awaiting approval")
        return service.reject_run(s, run, body.approver or actor, body.reason).model_dump(mode="json")

    # ---- memory ------------------------------------------------------------

    @app.get("/api/memory")
    def list_memory(request: Request, tier: str = "", actor: str = Depends(get_actor)):
        items = st(request).all_memory()
        if tier:
            items = [m for m in items if m.tier == tier]
        return [m.model_dump(mode="json") for m in items]

    @app.post("/api/memory", status_code=201)
    def add_memory(body: MemoryIn, request: Request, actor: str = Depends(get_actor)):
        s = st(request)
        if body.tier not in ("private", "team", "org"):
            raise HTTPException(422, "tier must be private|team|org")
        item = MemoryItem(tier=body.tier, owner=body.owner or actor,
                          content=body.content, created_by=actor)
        s.put_memory(item)
        audit(s, "memory_added", actor=actor,
              summary=f"memory added to {body.tier} tier", detail=body.content)
        return item.model_dump(mode="json")


    @app.post("/api/memory/{item_id}/promote")
    def promote_memory(item_id: str, body: PromoteIn, request: Request,
                       actor: str = Depends(get_actor)):
        """Promotion private -> team is human-approved only, and audited."""
        s = st(request)
        item = s.get_memory(item_id)
        if item is None:
            raise HTTPException(404, "memory item not found")
        approver = body.approver or actor
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
        audit_slice = [e.model_dump(mode="json") for e in s.all_audit() if e.run_id == run_id]
        pack = {
            "run": run.model_dump(mode="json"),
            "approvals": [a.model_dump(mode="json") for a in s.all_applied()
                          if a.run_id == run_id],
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
