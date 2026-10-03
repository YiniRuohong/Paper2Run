from __future__ import annotations

import hashlib
import json
from typing import Any
from fastapi import APIRouter, Request, Body
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from p2r_persistence.models import (
    ExperimentRecord, SpecRevisionRecord, ConfirmationRecord, PaperRecord,
    ExtractionRecord, AuditLogRecord, IdempotencyKey,
)
from p2r_persistence.repositories import IdempotencyRepository
from p2r_orchestration.service import ServiceError
from ..auth import require_project_access, csrf_token, validate_csrf, validate_origin
from ..dependencies import actor_id
from ..demo_receipts import lookup as receipt_lookup, save as receipt_save

router = APIRouter()
POLICY = {"version":"paper2run-policy-v1","max_cost_micros":100000000,"max_execution_seconds":86400}

def err(code, message, status=422, details=None):
    e=ServiceError(message, details=details or {}); e.code=code; e.status=status; raise e

def envelope(request, data): return {"data":data,"meta":{"request_id":request.state.request_id}}
def canon(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False).encode()
def digest(value): return hashlib.sha256(canon(value)).hexdigest()
def policy_hash(): return digest(POLICY)

def write_check(request, db, project_id, user_id):
    require_project_access(db,user_id,project_id,action="write")
    if not request.app.state.runtime.get("development_auth"):
        allowed=request.app.state.runtime.get("allowed_origin")
        if not allowed: err("CSRF_FAILED","origin rejected",403)
        validate_origin(request.headers.get("origin"),allowed)
        secret=request.cookies.get("p2r_session")
        if not secret: err("SESSION_REQUIRED","session required",401)
        validate_csrf(request.headers.get("x-csrf-token"),csrf_token(secret))

def idem(request, db, scope, body):
    cached = receipt_lookup(request, db, body)
    if cached is not None:
        return cached
    key=request.headers.get("idempotency-key")
    if not key: err("VALIDATION_ERROR","Idempotency-Key is required")
    claim=IdempotencyRepository(db).claim(scope=scope,key=key,body=body)
    if claim.conflict: err("IDEMPOTENCY_CONFLICT","idempotency key was used with a different request body",409)
    if claim.record.response_json is not None: return claim.record.response_json
    if not claim.claimed: err("IDEMPOTENCY_IN_PROGRESS","idempotency key is already in progress",409)
    return None

class CreateExperiment(BaseModel):
    model_config=ConfigDict(extra="forbid")
    paper_id:str; extraction_id:str; candidate_experiment_id:str; recipe_id:str; recipe_version:str
class CreateRevision(BaseModel):
    model_config=ConfigDict(extra="forbid")
    experiment_id:str; base_revision_id:str|None=None; spec:dict[str,Any]
class ConfirmRevision(BaseModel):
    model_config=ConfigDict(extra="forbid")
    spec_hash:str; policy_hash:str; accepted_issue_codes:list[str]=[]; accept_budget:bool

def experiment_json(row):
    return {"experiment_id":row.id,"project_id":row.project_id,"paper_id":row.paper_id,"status":row.status,"head_revision_id":row.head_revision_id,"active_run_id":None,"latest_run_id":None,"repairs_used":row.repairs_used,"repairs_limit":row.repairs_limit,"created_at":row.created_at.isoformat()}
def revision_json(row, db=None):
    confirmation = None
    if db is not None:
        conf = db.scalar(select(ConfirmationRecord).where(ConfirmationRecord.revision_id == row.id).order_by(ConfirmationRecord.confirmed_at.desc()))
        if conf:
            confirmation = {"confirmation_id": conf.id, "revision_id": row.id, "spec_hash": conf.spec_hash, "confirmed_by": conf.actor_id, "confirmed_at": conf.confirmed_at.isoformat()}
    return {"id":row.id,"experiment_id":row.experiment_id,"revision_no":row.revision_no,"spec_hash":row.spec_hash,"policy_hash":row.policy_hash,"spec":row.spec_json,"validation":row.validation_json,"comparison_precheck":row.comparison_json,"budget_estimate":row.budget_json,"confirmation":confirmation,"is_current":False,"created_at":row.created_at.isoformat()}

def draft_for(db, exp):
    ext=db.get(ExtractionRecord,exp.extraction_id)
    issues=[]; candidate=None
    if ext is None: issues.append({"code":"EXTRACTION_MISSING","message":"extraction is missing","path":"extraction_id","severity":"blocking"})
    else:
        candidates=(ext.payload_json or {}).get("candidate_experiments",[])
        candidate=next((c for c in candidates if c.get("id")==exp.candidate_experiment_id),None)
        if candidate is None: issues.append({"code":"CANDIDATE_MISSING","message":"candidate is missing","path":"candidate_experiment_id","severity":"blocking"})
        elif candidate.get("execution_support")=="unsupported": issues.append({"code":"CANDIDATE_UNSUPPORTED","message":"candidate is unsupported","path":"candidate_experiment_id","severity":"blocking"})
    spec={"schema_version":"1.0","paper_id":exp.paper_id,"extraction_id":exp.extraction_id,"candidate_experiment_id":exp.candidate_experiment_id,"recipe":{"id":exp.recipe_id,"version":exp.recipe_version},"mode":"smoke","method":{"family":(candidate or {}).get("label","unknown"),"variant":None,"must_include":[]},"dataset":{"id":None,"version":None,"split_policy_id":None,"train_subset_fraction":None},"training":{"seed":None,"batch_size":None,"max_steps":None,"max_epochs":None,"optimizer":None,"learning_rate":None,"weight_decay":None},"targets":[],"budget":{"max_model_calls":1,"max_tokens":1000,"max_execution_seconds":POLICY["max_execution_seconds"],"currency":"CNY","max_cost_micros":POLICY["max_cost_micros"]},"recipe_config":{},"assumptions":[],"user_changes":[]}
    head=db.get(SpecRevisionRecord,exp.head_revision_id) if exp.head_revision_id else None
    if head: spec=head.spec_json
    return {"experiment_id":exp.id,"base_revision_id":head.id if head else None,"policy_hash":policy_hash(),"spec":spec,"editable_fields":[],"issues":issues,"comparison_precheck":{"status":"unresolved","reasons":[]}}

@router.post("/projects/{project_id}/experiments",status_code=201)
async def create_experiment(project_id:str, request:Request, body:CreateExperiment=Body(...)):
    runtime=request.app.state.runtime; uid=actor_id(request); raw=await request.body()
    with runtime["session_factory"]() as db:
        write_check(request,db,project_id,uid); cached=idem(request,db,f"experiment:{uid}:{project_id}",raw)
        if cached: return envelope(request,cached)
        paper=db.get(PaperRecord,body.paper_id)
        if not paper or paper.project_id!=project_id: err("RESOURCE_NOT_FOUND","resource not found",404)
        ext=db.get(ExtractionRecord,body.extraction_id)
        if not ext or ext.paper_id!=paper.id: err("RESOURCE_NOT_FOUND","resource not found",404)
        candidate=next((c for c in (ext.payload_json or {}).get("candidate_experiments",[]) if c.get("id")==body.candidate_experiment_id),None)
        if candidate is None or candidate.get("recipe_id")!=body.recipe_id or candidate.get("recipe_version")!=body.recipe_version: err("CANDIDATE_INVALID","candidate does not match persisted extraction")
        row=ExperimentRecord(project_id=project_id,paper_id=paper.id,extraction_id=ext.id,candidate_experiment_id=body.candidate_experiment_id,recipe_id=body.recipe_id,recipe_version=body.recipe_version)
        db.add(row); db.flush(); out=experiment_json(row); cached_key=request.headers["idempotency-key"]; claim=db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope==f"experiment:{uid}:{project_id}", IdempotencyKey.key==cached_key)); claim.response_json=out; receipt_save(request, db, raw, project_id, out); db.commit(); return envelope(request,out)

@router.get("/projects/{project_id}/experiments")
def list_experiments(project_id:str,request:Request):
    with request.app.state.runtime["session_factory"]() as db:
        require_project_access(db,actor_id(request),project_id); rows=db.scalars(select(ExperimentRecord).where(ExperimentRecord.project_id==project_id).order_by(ExperimentRecord.created_at)).all(); return envelope(request,[experiment_json(x) for x in rows])

@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id:str,request:Request):
    with request.app.state.runtime["session_factory"]() as db:
        row=db.get(ExperimentRecord,experiment_id)
        if not row: err("RESOURCE_NOT_FOUND","resource not found",404)
        require_project_access(db,actor_id(request),row.project_id); return envelope(request,experiment_json(row))

@router.get("/experiments/{experiment_id}/draft")
def get_draft(experiment_id:str,request:Request):
    with request.app.state.runtime["session_factory"]() as db:
        row=db.get(ExperimentRecord,experiment_id)
        if not row: err("RESOURCE_NOT_FOUND","resource not found",404)
        require_project_access(db,actor_id(request),row.project_id); return envelope(request,draft_for(db,row))

@router.get("/experiments/{experiment_id}/revisions")
def list_revisions(experiment_id:str,request:Request):
    with request.app.state.runtime["session_factory"]() as db:
        exp=db.get(ExperimentRecord,experiment_id)
        if not exp: err("RESOURCE_NOT_FOUND","resource not found",404)
        require_project_access(db,actor_id(request),exp.project_id); rows=db.scalars(select(SpecRevisionRecord).where(SpecRevisionRecord.experiment_id==experiment_id).order_by(SpecRevisionRecord.revision_no)).all(); return envelope(request,[revision_json(x, db) for x in rows])

@router.post("/projects/{project_id}/spec-revisions",status_code=201)
async def create_revision(project_id:str,request:Request,body:CreateRevision=Body(...)):
    raw=await request.body(); uid=actor_id(request)
    with request.app.state.runtime["session_factory"]() as db:
        write_check(request,db,project_id,uid); cached=idem(request,db,f"revision:{uid}:{project_id}",raw)
        if cached:return envelope(request,cached)
        exp=db.get(ExperimentRecord,body.experiment_id)
        if not exp or exp.project_id!=project_id: err("RESOURCE_NOT_FOUND","resource not found",404)
        head=db.get(SpecRevisionRecord,exp.head_revision_id) if exp.head_revision_id else None
        if body.base_revision_id != (head.id if head else None): err("REVISION_CONFLICT","base revision is stale",409)
        h=digest(body.spec)
        draft=draft_for(db,exp)
        row=SpecRevisionRecord(experiment_id=exp.id,revision_no=(head.revision_no+1 if head else 1),spec_hash=h,policy_hash=policy_hash(),spec_json=body.spec,validation_json={"issues":draft["issues"]},comparison_json=draft["comparison_precheck"],budget_json={"currency":"CNY","amount_micros":None,"duration_seconds":None,"basis":"draft"})
        db.add(row); db.flush(); exp.head_revision_id=row.id; out=revision_json(row, db); claim = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope==f"revision:{uid}:{project_id}", IdempotencyKey.key==request.headers["idempotency-key"])); claim.response_json=out; receipt_save(request, db, raw, project_id, out); db.commit(); return envelope(request,out)

@router.get("/spec-revisions/{revision_id}")
def get_revision(revision_id:str,request:Request):
    with request.app.state.runtime["session_factory"]() as db:
        row=db.get(SpecRevisionRecord,revision_id)
        if not row:err("RESOURCE_NOT_FOUND","resource not found",404)
        exp=db.get(ExperimentRecord,row.experiment_id); require_project_access(db,actor_id(request),exp.project_id); return envelope(request,revision_json(row, db))

@router.post("/spec-revisions/{revision_id}/confirm",status_code=201)
async def confirm_revision(revision_id:str,request:Request,body:ConfirmRevision=Body(...)):
    raw=await request.body(); uid=actor_id(request)
    with request.app.state.runtime["session_factory"]() as db:
        row=db.get(SpecRevisionRecord,revision_id)
        if not row:err("RESOURCE_NOT_FOUND","resource not found",404)
        exp=db.get(ExperimentRecord,row.experiment_id); write_check(request,db,exp.project_id,uid); cached=idem(request,db,f"confirmation:{uid}:{revision_id}",raw)
        if cached:return envelope(request,cached)
        if body.spec_hash!=row.spec_hash or digest(row.spec_json)!=row.spec_hash:err("HASH_MISMATCH","spec hash mismatch",409)
        if body.policy_hash!=row.policy_hash:err("POLICY_MISMATCH","policy hash mismatch",409)
        issues=(row.validation_json or {}).get("issues",[])
        if any(x.get("severity")=="blocking" for x in issues):err("BLOCKING_ISSUES","blocking issues prevent confirmation")
        if body.accept_budget is not True:err("BUDGET_NOT_ACCEPTED","budget must be accepted")
        conf=ConfirmationRecord(revision_id=row.id,actor_id=uid,spec_hash=row.spec_hash,policy_hash=row.policy_hash,accepted_issue_codes=body.accepted_issue_codes); db.add(conf); db.flush(); exp.status="ready"; out={"confirmation_id":conf.id,"revision_id":row.id,"spec_hash":row.spec_hash,"confirmed_by":uid,"confirmed_at":conf.confirmed_at.isoformat()}; claim = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope==f"confirmation:{uid}:{revision_id}", IdempotencyKey.key==request.headers["idempotency-key"])); claim.response_json=out; receipt_save(request, db, raw, exp.project_id, out); db.commit(); return envelope(request,out)
