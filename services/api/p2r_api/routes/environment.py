from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from p2r_orchestration.environment_checker import EnvironmentChecker
from p2r_orchestration.service import ServiceError
from p2r_orchestration.ssh_executor import (
    FakeSSHVerifier,
    SSHRecipe,
    SSHVerificationStatus,
    SSHTarget,
    verify_target,
)
from p2r_persistence.models import (
    EnvironmentCheckRecord,
    ExperimentRecord,
    SSHVerificationRecord,
    SpecRevisionRecord,
)
from p2r_persistence.repositories import IdempotencyRepository

from ..auth import require_project_access
from ..demo_receipts import lookup as receipt_lookup, save as receipt_save
from ..dependencies import actor_id
from .experiments import write_check

router = APIRouter()


def err(code: str, message: str, status: int = 422, details: dict[str, Any] | None = None):
    error = ServiceError(message, details=details or {})
    error.code = code
    error.status = status
    raise error


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request.state.request_id}}


class EnvironmentCheckCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str
    requirements: dict[str, Any] = Field(default_factory=dict)


def check_json(row: EnvironmentCheckRecord) -> dict[str, Any]:
    return {
        "environment_check_id": row.id,
        "project_id": row.project_id,
        "experiment_id": row.experiment_id,
        "revision_id": row.revision_id,
        "decision": row.decision,
        "status": row.decision,
        "checker_version": row.checker_version,
        "requirements": row.requirements_json,
        "items": row.items_json,
        "repair_suggestions": row.suggestions_json,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/projects/{project_id}/experiments/{experiment_id}/environment-checks", status_code=201)
async def create_environment_check(
    project_id: str,
    experiment_id: str,
    request: Request,
    body: EnvironmentCheckCreate = Body(...),
):
    raw = await request.body()
    uid = actor_id(request)
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        write_check(request, db, project_id, uid)
        cached = receipt_lookup(request, db, raw)
        if cached is not None:
            return envelope(request, cached)
        key = request.headers.get("idempotency-key")
        if not key:
            err("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(
            scope=f"environment-check:{uid}:{project_id}:{experiment_id}", key=key, body=raw
        )
        if claim.conflict:
            err("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None:
            return envelope(request, claim.record.response_json)
        if not claim.claimed:
            err("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)

        experiment = db.get(ExperimentRecord, experiment_id)
        revision = db.get(SpecRevisionRecord, body.revision_id)
        if not experiment or experiment.project_id != project_id or not revision or revision.experiment_id != experiment_id:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        candidate_supported = experiment.status != "unsupported"
        checker = runtime.get("environment_checker") or EnvironmentChecker()
        result = checker.check(
            recipe_supported=candidate_supported,
            requirements=body.requirements,
        )
        row = EnvironmentCheckRecord(
            project_id=project_id,
            experiment_id=experiment_id,
            revision_id=revision.id,
            decision=result.decision.value,
            checker_version=result.checker_version,
            requirements_json=body.requirements,
            items_json=[item.model_dump(mode="json") for item in result.items],
            suggestions_json=result.repair_suggestions,
        )
        db.add(row)
        db.flush()
        out = check_json(row)
        claim.record.response_json = out
        receipt_save(request, db, raw, project_id, out)
        db.commit()
        return envelope(request, out)


@router.get("/environment-checks/{environment_check_id}")
def get_environment_check(environment_check_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        row = db.get(EnvironmentCheckRecord, environment_check_id)
        if row is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        require_project_access(db, actor_id(request), row.project_id)
        return envelope(request, check_json(row))


@router.get("/projects/{project_id}/experiments/{experiment_id}/environment-checks")
def list_environment_checks(project_id: str, experiment_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        require_project_access(db, actor_id(request), project_id)
        rows = db.scalars(
            select(EnvironmentCheckRecord)
            .where(
                EnvironmentCheckRecord.project_id == project_id,
                EnvironmentCheckRecord.experiment_id == experiment_id,
            )
            .order_by(EnvironmentCheckRecord.created_at.desc())
        ).all()
        return envelope(request, [check_json(row) for row in rows])


class SSHVerificationCreate(BaseModel):
    """Manual, user-selected SSH verification request.

    Only a structured target is accepted: no password, no private key
    material, no arbitrary command/argv. ``user_selected`` must be explicitly
    true — SSH is never an automatic fallback from a blocked check.
    """

    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    user_selected: bool = False


def ssh_verification_json(row: SSHVerificationRecord) -> dict[str, Any]:
    return {
        "ssh_verification_id": row.id,
        "environment_check_id": row.environment_check_id,
        "project_id": row.project_id,
        "experiment_id": row.experiment_id,
        "revision_id": row.revision_id,
        "status": row.status,
        "target": row.target_json,
        "summary": row.summary_json,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _ssh_verifier(request: Request):
    return request.app.state.runtime.get("ssh_verifier") or FakeSSHVerifier()


@router.post("/environment-checks/{environment_check_id}/ssh-verifications", status_code=201)
async def create_ssh_verification(environment_check_id: str, request: Request, body: SSHVerificationCreate = Body(...)):
    raw = await request.body()
    uid = actor_id(request)
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        check = db.get(EnvironmentCheckRecord, environment_check_id)
        if check is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        # Write permission + CSRF/origin checks for this project.
        write_check(request, db, check.project_id, uid)
        key = request.headers.get("idempotency-key")
        if not key:
            err("VALIDATION_ERROR", "Idempotency-Key is required")
        scope = f"ssh-verification:{uid}:{check.project_id}:{environment_check_id}"
        claim = IdempotencyRepository(db).claim(scope=scope, key=key, body=raw)
        if claim.conflict:
            err("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None:
            return envelope(request, claim.record.response_json)
        if not claim.claimed:
            err("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)

        # SSH is only a manual diversion from a blocked local check; it is
        # never an automatic fallback.
        if check.decision != "blocked":
            err("SSH_NOT_AVAILABLE", "ssh verification is only available for blocked environment checks", 409)
        if body.user_selected is not True:
            err("VALIDATION_ERROR", "user_selected must be explicitly true to verify an ssh target")

        experiment = db.get(ExperimentRecord, check.experiment_id)
        if experiment is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        recipe_supported = experiment.status != "unsupported"

        try:
            target = SSHTarget.model_validate(body.target)
        except Exception:
            err("VALIDATION_ERROR", "invalid ssh target")

        if not recipe_supported:
            err("SSH_RECIPE_UNSUPPORTED", "the recipe is not supported for ssh execution", 409)

        recipe = SSHRecipe(recipe_id=experiment.recipe_id, recipe_version=experiment.recipe_version)
        try:
            result = verify_target(_ssh_verifier(request), target, recipe)
        except Exception:
            err("SSH_VERIFICATION_FAILED", "ssh verification failed", 409)

        if result.status is not SSHVerificationStatus.VERIFIED:
            err("SSH_VERIFICATION_FAILED", "ssh verification failed", 409)

        row = SSHVerificationRecord(
            environment_check_id=check.id,
            project_id=check.project_id,
            experiment_id=check.experiment_id,
            revision_id=check.revision_id,
            status="awaiting_confirmation",
            target_json=target.safe_projection(),
            summary_json=dict(result.summary),
        )
        db.add(row)
        db.flush()
        out = ssh_verification_json(row)
        claim.record.response_json = out
        db.commit()
        return envelope(request, out)


@router.get("/ssh-verifications/{ssh_verification_id}")
def get_ssh_verification(ssh_verification_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        row = db.get(SSHVerificationRecord, ssh_verification_id)
        if row is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        require_project_access(db, actor_id(request), row.project_id)
        return envelope(request, ssh_verification_json(row))
