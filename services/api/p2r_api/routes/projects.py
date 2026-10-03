from __future__ import annotations

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from p2r_persistence.models import ProjectRecord, MembershipRecord, UserRecord, AuditLogRecord, utcnow, IdempotencyKey
from p2r_persistence.repositories import IdempotencyRepository
from p2r_orchestration.service import ServiceError
from ..auth import require_project_access, ForbiddenError, csrf_token, validate_csrf, validate_origin
from ..dependencies import actor_id

router = APIRouter()


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def nonempty(cls, values):
        if not isinstance(values, dict) or not values or any(v is None for v in values.values()):
            raise ValueError("at least one non-null field required")
        if "name" in values and (not isinstance(values["name"], str) or not values["name"].strip()):
            raise ValueError("name must not be blank")
        return values


class ProjectService:
    def __init__(self, db: Session): self.db = db

    def create_project(self, user_id: str, body: ProjectCreate) -> ProjectRecord:
        user = self.db.get(UserRecord, user_id)
        if user is None or user.status != "active" or not user.can_create_project: raise ForbiddenError("project creation forbidden")
        row = ProjectRecord(name=body.name, description=body.description, created_by=user_id)
        try:
            self.db.add(row); self.db.flush()
            self.db.add(MembershipRecord(project_id=row.id, user_id=user_id, role="owner"))
            self.db.add(AuditLogRecord(actor_user_id=user_id, action="project_created", resource_type="project", resource_id=row.id, details_json={"version": 1}))
            self.db.commit()
        except Exception:
            self.db.rollback(); raise
        self.db.refresh(row)
        return row

    def require_project(self, user_id: str, project_id: str, *, action: str = "read"):
        return require_project_access(self.db, user_id, project_id, action=action)

    def patch_project(self, user_id: str, project_id: str, body: ProjectPatch, *, expected_version: int | None = None) -> ProjectRecord:
        if expected_version is None:
            error = ServiceError("If-Match is required"); error.status = 409; error.code = "PRECONDITION_REQUIRED"; raise error
        row, _ = self.require_project(user_id, project_id, action="write")
        values = {"version": expected_version + 1, "updated_at": utcnow()}
        if body.name is not None: values["name"] = body.name
        if body.description is not None: values["description"] = body.description
        result = self.db.execute(update(ProjectRecord).where(ProjectRecord.id == project_id, ProjectRecord.version == expected_version).values(**values))
        if result.rowcount != 1:
            self.db.rollback(); error = ServiceError("project version conflict"); error.status = 409; error.code = "REVISION_CONFLICT"; raise error
        self.db.add(AuditLogRecord(actor_user_id=user_id, action="project_edited", resource_type="project", resource_id=project_id, details_json={"version": expected_version + 1}))
        self.db.commit(); self.db.refresh(row); return row


def _envelope(request: Request, data):
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def _error(code, message, status=422):
    error = ServiceError(message); error.code = code; error.status = status; raise error


def _project_json(row: ProjectRecord, role: str) -> dict:
    return {"id": row.id, "name": row.name, "description": row.description, "role": role,
            "version": row.version, "deletion_state": row.deletion_state,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def _write_check(request: Request, db: Session, project_id: str | None, user_id: str):
    if project_id is not None:
        require_project_access(db, user_id, project_id, action="write")
    if not request.app.state.runtime.get("development_auth"):
        allowed = request.app.state.runtime.get("allowed_origin")
        if not allowed: _error("CSRF_FAILED", "origin rejected", 403)
        validate_origin(request.headers.get("origin"), allowed)
        secret = request.cookies.get("p2r_session")
        if not secret: _error("SESSION_REQUIRED", "session required", 401)
        validate_csrf(request.headers.get("x-csrf-token"), csrf_token(secret))


def _if_match(value: str | None) -> int:
    if not value: _error("PRECONDITION_REQUIRED", "If-Match is required", 409)
    try:
        token = value.strip().strip('"')
        return int(token.removeprefix("v"))
    except ValueError:
        _error("REVISION_CONFLICT", "invalid If-Match", 409)


def _with_etag(request: Request, payload: dict, version: int):
    return JSONResponse(_envelope(request, payload), headers={"ETag": f'"{version}"'})


@router.get("/projects")
def list_projects(request: Request):
    uid = actor_id(request)
    with request.app.state.runtime["session_factory"]() as db:
        rows = db.execute(select(ProjectRecord, MembershipRecord.role).join(MembershipRecord, MembershipRecord.project_id == ProjectRecord.id).where(MembershipRecord.user_id == uid).order_by(ProjectRecord.created_at, ProjectRecord.id)).all()
        return _envelope(request, [_project_json(row, role) for row, role in rows])


@router.post("/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreate = Body(...)):
    uid = actor_id(request); raw = await request.body()
    with request.app.state.runtime["session_factory"]() as db:
        _write_check(request, db, None, uid)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"project-create:{uid}", key=key, body=raw)
        if claim.conflict: _error("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None: return _envelope(request, claim.record.response_json)
        if not claim.claimed: _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)
        row = ProjectService(db).create_project(uid, body)
        out = _project_json(row, "owner"); claim.record.response_json = out; db.commit()
        return _with_etag(request, out, row.version)


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        row, membership = ProjectService(db).require_project(actor_id(request), project_id)
        return _with_etag(request, _project_json(row, membership.role), row.version)


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, request: Request, body: ProjectPatch = Body(...)):
    uid = actor_id(request); raw = await request.body()
    with request.app.state.runtime["session_factory"]() as db:
        _write_check(request, db, project_id, uid)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"project-patch:{uid}:{project_id}", key=key, body=raw)
        if claim.conflict: _error("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None: return _envelope(request, claim.record.response_json)
        if not claim.claimed: _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)
        row = ProjectService(db).patch_project(uid, project_id, body, expected_version=_if_match(request.headers.get("if-match")))
        _, membership = ProjectService(db).require_project(uid, project_id)
        out = _project_json(row, membership.role); claim.record.response_json = out; db.commit()
        return _with_etag(request, out, row.version)
