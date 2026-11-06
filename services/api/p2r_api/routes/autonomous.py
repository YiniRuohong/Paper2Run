"""Autonomous API. Shell-capable writes always validate Origin and authorization."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from p2r_persistence.autonomous import AutonomousTaskRecord as Task
from p2r_orchestration.autonomous.evidence import export_zip
from p2r_orchestration.autonomous import files as task_file_access
from ..auth import require_project_access, validate_origin, validate_csrf, csrf_token
from ..dependencies import actor_id
from .papers import _claim, _envelope, _error, _save

router = APIRouter(prefix="/autonomous")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Profile(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api: Literal["anthropic-messages", "openai-completions"]
    auth_mode: Literal["api-key", "none"] = "api-key"
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    credential_ref: str | None = Field(default=None, max_length=256)
    reuse_credentials_from: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    context_window: int = Field(default=128000, gt=0, le=10000000)
    max_tokens: int = Field(default=4096, gt=0, le=1000000)


class Configuration(Strict):
    config: dict


class Authorization(Strict):
    confirmed: Literal[True]


class CreateTask(Strict):
    objective: str = Field(min_length=1, max_length=8192)
    paper_version_id: str | None = Field(default=None, max_length=128)
    protocol: dict | None = None
    reference_values: dict | None = None
    reference_scope: str | None = Field(default=None, min_length=1, max_length=8192)
    reference_sources_confirmed: Literal[True] | None = None
    authorization: Authorization


class Control(Strict):
    action: Literal["pause", "resume", "stop", "message"]
    message: str | None = Field(default=None, max_length=16000)


class Decision(Strict):
    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=4000)


class ImportCapability(Strict):
    source_path: str = Field(min_length=1, max_length=2048)


class CapabilityState(Strict):
    enabled: bool


def service(request):
    return request.app.state.runtime["autonomous_service"]


def write_guard(request):
    runtime = request.app.state.runtime
    allowed = runtime.get("allowed_origin")
    if not allowed:
        _error("CSRF_FAILED", "autonomous writes require a configured allowed origin", status=403)
    validate_origin(request.headers.get("origin"), allowed)
    if not runtime.get("development_auth"):
        secret = request.cookies.get("p2r_session")
        if not secret:
            _error("SESSION_REQUIRED", "session required", status=401)
        validate_csrf(request.headers.get("x-csrf-token"), csrf_token(secret))
    # In loopback demo there is no auth cookie; JSON + required custom header
    # + strict Origin prevent browser cross-site shell submission.
    if not request.headers.get("idempotency-key"):
        _error("VALIDATION_ERROR", "Idempotency-Key is required")
    if len(request.headers["idempotency-key"]) > 128:
        _error("VALIDATION_ERROR", "Idempotency-Key exceeds limit")
    # Same rule as the target probe: another loopback port or a sibling subdomain
    # is same-site, not same-origin. The launcher page and the Vite proxy send same-origin.
    if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
        _error("CSRF_FAILED", "cross-site autonomous mutation refused", status=403)


async def mutate(request, model, action, *, project_id=None, task_id=None, access="write"):
    actor = actor_id(request)
    write_guard(request)
    raw = await request.body()
    if len(raw) > 2 * 1024 * 1024:
        _error("VALIDATION_ERROR", "request exceeds size limit")
    try:
        payload = model.model_validate_json(raw).model_dump(mode="json")
    except (ValidationError, ValueError):
        _error("VALIDATION_ERROR", "invalid autonomous request")
    svc = service(request)
    if model is not Profile:
        svc.profiles.reject_credentials(payload)
    launch_id, stop_id = None, None
    with svc.lock, svc.sessions() as db:
        if task_id:
            task = svc.row(db, task_id)
            project_id = task.project_id
        if project_id:
            require_project_access(db, actor, project_id, action=access)
        scope = f"auto:{actor}:{request.method}:{request.url.path}"
        key = request.headers["idempotency-key"]
        # Conditional version is part of idempotency identity, not just JSON body.
        claim = _claim(request, db, scope=scope, key=key, body=raw + b"\n" + request.headers.get("if-match", "").encode())
        if claim is not None:
            if model is Profile:
                # Identical replay re-injects the runtime-only key after a restart;
                # persisted idempotency response never acts as a credential store.
                svc.profiles.put(actor, payload)
            if model is Control and payload["action"] == "stop":
                # A cached admission is not proof of completed cancellation.
                # Reconcile interrupted/unknown stops under the original key.
                result = svc.control(db, task_id, "stop")
                if result.state != "stopped":
                    stop_id = task_id
            else:
                return _envelope(request, claim)
        else:
            try:
                result = action(svc, db, actor, payload)
            except (ValueError, TypeError, OSError):
                _error("VALIDATION_ERROR", "autonomous configuration or capability was rejected")
        if isinstance(result, Task):
            if result.state == "queued":
                launch_id = result.id
            if result.state == "stopping":
                stop_id = result.id
            result = svc.public(result)
        _save(request, db, scope=scope, key=key, response=result)
        db.commit()
    if stop_id:
        svc.finish_stop(stop_id)
        with svc.lock, svc.sessions() as db:
            result = svc.public(svc.row(db, stop_id))
            _save(request, db, scope=scope, key=key, response=result)
            db.commit()
    if launch_id:
        svc.launch(launch_id, payload.get("message"))
    return _envelope(request, result)


@router.get("/capabilities")
def capabilities(request: Request):
    actor_id(request)
    svc = service(request)
    with svc.lock:
        return _envelope(request, svc.registry.list())


@router.post("/capabilities/import")
async def import_capability(request: Request):
    return await mutate(request, ImportCapability, lambda s, db, actor, p: s.registry.import_local(Path(p["source_path"])))


@router.put("/capabilities/{capability_id}")
async def enable_capability(capability_id: str, request: Request):
    return await mutate(request, CapabilityState, lambda s, db, actor, p: s.registry.set_enabled(capability_id, p["enabled"]))


@router.post("/profiles", status_code=201)
async def profile_create(request: Request):
    return await mutate(request, Profile, lambda s, db, actor, p: s.profiles.put(actor, p))


@router.get("/profiles")
def profiles(request: Request):
    return _envelope(request, service(request).profiles.list(actor_id(request)))


@router.get("/projects/{project_id}/config")
def get_config(project_id: str, request: Request):
    actor, svc = actor_id(request), service(request)
    with svc.lock, svc.sessions() as db:
        require_project_access(db, actor, project_id)
        row = svc.latest_config(db, project_id)
        if row is None:
            _error("RESOURCE_NOT_FOUND", "project configuration not found", status=404)
        return _envelope(request, {"version": row.version, "config": row.config_json})


@router.get("/projects/{project_id}/target/probe")
def probe_target(project_id: str, request: Request):
    """C6: capabilities of the latest saved target. Observed facts only; never creates a container."""
    _strict_query(request, ())
    # It runs host commands (docker, ssh): another site, including another
    # loopback port or a sibling subdomain (same-site), must not trigger it blind.
    if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
        _error("CSRF_FAILED", "cross-site target probe refused", status=403)
    actor, svc = actor_id(request), service(request)
    with svc.lock, svc.sessions() as db:
        require_project_access(db, actor, project_id)
        row = svc.latest_config(db, project_id)
        if row is None:
            _error("RESOURCE_NOT_FOUND", "project configuration not found", status=404)
        config = json.loads(json.dumps(row.config_json))
    # Docker/SSH calls run outside the service lock (bounded to about 20 s).
    return _envelope(request, svc.probe_target(project_id, config))


@router.put("/projects/{project_id}/config")
async def put_config(project_id: str, request: Request):
    return await mutate(request, Configuration,
        lambda s, db, actor, p: s.put_config(db, project_id, p["config"], request.headers.get("if-match")), project_id=project_id)


@router.post("/projects/{project_id}/tasks", status_code=202)
async def create_task(project_id: str, request: Request):
    return await mutate(request, CreateTask, lambda s, db, actor, p: s.create_task(db, project_id, actor, p),
                        project_id=project_id, access="execute")


@router.get("/projects/{project_id}/tasks")
def tasks(project_id: str, request: Request):
    actor, svc = actor_id(request), service(request)
    with svc.lock, svc.sessions() as db:
        require_project_access(db, actor, project_id)
        rows = db.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc()).limit(200)).all()
        return _envelope(request, [svc.public(row) for row in rows])


def read_task(request, task_id, action):
    actor, svc = actor_id(request), service(request)
    with svc.lock, svc.sessions() as db:
        row = svc.row(db, task_id)
        require_project_access(db, actor, row.project_id)
        return action(svc, db, row)


@router.get("/tasks/{task_id}")
def task(task_id: str, request: Request):
    return _envelope(request, read_task(request, task_id, lambda s, db, r: s.public(r)))


@router.get("/tasks/{task_id}/events")
def events(task_id: str, request: Request, after: int = Query(default=0, ge=0)):
    return _envelope(request, read_task(request, task_id, lambda s, db, r: s.events(db, task_id, after)))


@router.post("/tasks/{task_id}/control")
async def control(task_id: str, request: Request):
    return await mutate(request, Control, lambda s, db, actor, p: s.control(db, task_id, p["action"], p.get("message")),
                        task_id=task_id, access="execute")


@router.post("/tasks/{task_id}/decisions/{decision_id}")
async def decide(task_id: str, decision_id: str, request: Request):
    return await mutate(request, Decision,
        lambda s, db, actor, p: s.decide(db, task_id, decision_id, p["action"], p.get("note")),
        task_id=task_id, access="execute")


@router.get("/tasks/{task_id}/export")
def export(task_id: str, request: Request):
    data = read_task(request, task_id, lambda s, db, r: export_zip(s, task_id))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="autonomous-{task_id}.zip"'})


def _strict_query(request, allowed):
    unknown = sorted(set(request.query_params) - set(allowed))
    if unknown:
        _error("VALIDATION_ERROR", "unknown query parameter", details={"parameters": unknown})
    if any(len(request.query_params.getlist(name)) > 1 for name in allowed):
        _error("VALIDATION_ERROR", "duplicate query parameter")


@router.get("/tasks/{task_id}/files")
def task_files(task_id: str, request: Request):
    _strict_query(request, ())
    # Snapshot under the service lock; filesystem IO happens outside it.
    snap = read_task(request, task_id, lambda s, db, r: task_file_access.snapshot(s, r))
    return _envelope(request, task_file_access.list_files(snap))


@router.get("/tasks/{task_id}/files/raw")
def task_file_raw(task_id: str, request: Request):
    _strict_query(request, ("source", "path"))
    source, path = request.query_params.get("source"), request.query_params.get("path")
    if source not in ("artifact", "workspace"):
        _error("VALIDATION_ERROR", "source must be artifact or workspace")
    task_file_access.validate_path(path)
    svc = service(request)
    snap = read_task(request, task_id, lambda s, db, r: task_file_access.snapshot(s, r))
    raw, media, filename = task_file_access.read_file(snap, source, path)
    task_file_access.refuse_secrets(svc, raw)
    content_type, headers = task_file_access.raw_headers(media, raw, filename)
    return Response(raw, headers={**headers, "Content-Type": content_type})
