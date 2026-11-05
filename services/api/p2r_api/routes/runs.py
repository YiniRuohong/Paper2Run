from __future__ import annotations

import hashlib, json, re
from typing import Any, Literal
from fastapi import APIRouter, Body, Request
from fastapi.responses import Response, StreamingResponse
from p2r_artifacts.store import ArtifactIntegrityError, ArtifactNotFoundError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from p2r_persistence.models import AttemptRecord, ConfirmationRecord, EnvironmentCheckRecord, ExecutionRecord, ExperimentRecord, ExportRecord, ExportItemRecord, IdempotencyKey, OperationRecord, ReportRecord, RunEventRecord, RunRecord, SpecRevisionRecord, RepairProposalRecord, SSHVerificationRecord, ExtractionRecord
from p2r_orchestration.execution_repository import ExecutionRepository
from p2r_orchestration.environment_checker import EnvironmentChecker
from p2r_orchestration.repair_repository import create_repair_proposal, list_repair_proposals
from p2r_persistence.repositories import IdempotencyRepository
from p2r_persistence.job_repository import JobRepository
from p2r_orchestration.service import ServiceError
from p2r_orchestration.export_jobs import MEDIA, FILENAMES
from p2r_orchestration.evidence_registry import EVIDENCE_EVENT_TYPE
from p2r_persistence.models import ExportArtifactRecord
from p2r_evaluation import EVALUATOR_VERSION, evaluate_predictions
from p2r_runner import run_fixture, FixtureRunnerError, is_registered_recipe, is_complete_sandbox_code_manifest, REAL_RECIPE_ID, REAL_RUNNER_ID, SANDBOX_RECIPE_ID, SANDBOX_RUNNER_ID, FIXTURE_RUNNER_ID
from ..auth import require_project_access
from .experiments import write_check
from ..dependencies import actor_id
from ..demo_receipts import lookup as receipt_lookup, save as receipt_save

router = APIRouter()

def err(code: str, message: str, status: int = 422, details: dict[str, Any] | None = None):
    ex = ServiceError(message, details=details or {}); ex.code = code; ex.status = status; raise ex

def envelope(request: Request, data: Any): return {"data": data, "meta": {"request_id": request.state.request_id}}
def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

class RunExecutionTarget(BaseModel):
    """Explicit execution target binding for a Run.

    ``confirmed`` must be true when binding a remote target: a Run is never
    created for a target the user has not explicitly confirmed. Credentials
    are never accepted here — only the id of an existing SSH verification.
    """

    model_config = ConfigDict(extra="forbid")
    kind: str | None = None
    ssh_verification_id: str | None = None
    confirmed: bool = False


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str | None = None
    spec_revision_id: str | None = None
    confirmation_id: str
    parent_run_id: str | None = None
    mode: str = "local-cpu"
    execution: RunExecutionTarget | None = None

    @property
    def resolved_revision_id(self) -> str:
        return self.spec_revision_id or self.revision_id or ""

class RepairProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    repair_ordinal: int
    base_code_hash: str
    patch_hash: str
    proposal_json: dict[str, Any]
    validation: dict[str, Any]
    status: str = "proposed"


class ExecutionHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner_id: str
    fencing_token: int
    lease_seconds: int = Field(gt=0, le=3600)


class ExecutionFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner_id: str
    fencing_token: int
    status: Literal["succeeded", "failed", "cancelled"]
    error: dict[str, Any] | None = None


class RunCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formats: list[str]
    include_checkpoint: bool = False

def _safe_run_error(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    safe = {
        key: value[key]
        for key in ("code", "message")
        if isinstance(value.get(key), str)
    }
    return safe or None


def run_json(row: RunRecord, last_seq: int | None = None) -> dict[str, Any]:
    return {"run_id": row.id, "id": row.id, "project_id": row.project_id, "experiment_id": row.experiment_id, "revision_id": row.revision_id, "spec_revision_id": row.revision_id, "status": row.status, "mode": row.mode, "manifest_hash": row.manifest_hash, "metrics_hash": row.metrics_hash, "report_hash": row.report_hash, "verdict": row.verdict, "error": _safe_run_error(row.error_json), "last_event_seq": last_seq if last_seq is not None else 0, "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}

def _require_supported_recipe(db, exp) -> None:
    """A Run may only be created for a registered recipe whose candidate is
    explicitly marked supported in the frozen extraction payload."""
    if exp.status == "unsupported":
        err("UNSUPPORTED_RECIPE", "the experiment recipe is unsupported", 422)
    extraction = db.get(ExtractionRecord, exp.extraction_id)
    candidates = extraction.payload_json.get("candidate_experiments") if extraction else None
    candidate = next(
        (c for c in candidates if isinstance(c, dict) and c.get("id") == exp.candidate_experiment_id),
        None,
    ) if isinstance(candidates, list) else None
    if candidate is None or candidate.get("execution_support") != "supported":
        err("UNSUPPORTED_RECIPE", "the candidate experiment is not explicitly supported", 422)
    if not is_registered_recipe(exp.recipe_id, exp.recipe_version):
        err("UNSUPPORTED_RECIPE", "the recipe is not registered with the controlled runner", 422)


def _resolve_execution_target(db, project_id: str, exp, rev, body: RunCreate):
    """Validate the explicit execution target binding before any Run exists.

    Returns the bound SSHVerificationRecord for ssh mode, else None. A local
    run needs no extra binding (the spec confirmation is its explicit local
    confirmation); an ssh run must bind a verification that is still
    ``awaiting_confirmation`` and carry an explicit confirmation flag.
    """
    execution = body.execution
    if body.mode == "local-cpu":
        if execution is None:
            return None
        if execution.kind not in (None, "local-cpu") or execution.ssh_verification_id is not None:
            err("VALIDATION_ERROR", "execution target does not match local-cpu mode", 422)
        return None
    if execution is None or not execution.ssh_verification_id:
        err("VALIDATION_ERROR", "ssh mode requires an ssh verification binding", 422)
    if execution.kind not in (None, "ssh"):
        err("VALIDATION_ERROR", "execution target does not match ssh mode", 422)
    if execution.confirmed is not True:
        err("EXECUTION_TARGET_NOT_CONFIRMED", "the execution target must be explicitly confirmed", 422)
    verification = db.get(SSHVerificationRecord, execution.ssh_verification_id)
    if (verification is None or verification.project_id != project_id
            or verification.experiment_id != exp.id or verification.revision_id != rev.id):
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    if verification.status != "awaiting_confirmation":
        err("SSH_VERIFICATION_REQUIRED", "ssh execution requires a verification awaiting explicit confirmation", 409)
    return verification


def _demo_environment_check(request: Request, db, project_id: str, exp, rev):
    """Guided-demo fixture path: run the real read-only checker once, record
    the result, and pass the same ready/blocked gate as any other Run. The
    demo never bypasses the gate and never fabricates a ready decision."""
    runtime = request.app.state.runtime
    checker = runtime.get("environment_checker") or EnvironmentChecker()
    result = checker.check(recipe_supported=(exp.status != "unsupported"), requirements={})
    row = EnvironmentCheckRecord(
        project_id=project_id, experiment_id=exp.id, revision_id=rev.id,
        decision=result.decision.value, checker_version=result.checker_version,
        requirements_json={},
        items_json=[item.model_dump(mode="json") for item in result.items],
        suggestions_json=result.repair_suggestions,
    )
    db.add(row); db.flush()
    return row


def _create_run(request: Request, project_id: str, experiment_id: str | None, body: RunCreate):
    raw = request.state._run_raw; uid = actor_id(request)
    with request.app.state.runtime["session_factory"]() as db:
        write_check(request, db, project_id, uid)
        cached = receipt_lookup(request, db, raw)
        if cached is not None: return envelope(request, cached)
        key = request.headers.get("idempotency-key")
        if not key: err("VALIDATION_ERROR", "Idempotency-Key is required")
        scope = f"run:{uid}:{project_id}:{experiment_id or 'root'}"
        claim = IdempotencyRepository(db).claim(scope=scope, key=key, body=raw)
        if claim.conflict: err("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None: return envelope(request, claim.record.response_json)
        if not claim.claimed: err("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)
        rev = db.get(SpecRevisionRecord, body.resolved_revision_id)
        exp = db.get(ExperimentRecord, experiment_id) if experiment_id else (db.get(ExperimentRecord, rev.experiment_id) if rev else None)
        if not rev or not exp or exp.project_id != project_id or rev.experiment_id != exp.id: err("RESOURCE_NOT_FOUND", "resource not found", 404)
        conf = db.get(ConfirmationRecord, body.confirmation_id)
        if not conf or conf.revision_id != rev.id: err("RESOURCE_NOT_FOUND", "resource not found", 404)
        if conf.spec_hash != rev.spec_hash or conf.policy_hash != rev.policy_hash or digest(rev.spec_json) != rev.spec_hash: err("HASH_MISMATCH", "revision hash mismatch", 409)
        if body.mode not in ("local-cpu", "ssh"): err("UNSUPPORTED_MODE", "only local-cpu and ssh execution modes are supported", 422)
        _require_supported_recipe(db, exp)
        ssh_verification = _resolve_execution_target(db, project_id, exp, rev, body)
        env_check = db.scalar(select(EnvironmentCheckRecord).where(EnvironmentCheckRecord.revision_id == rev.id).order_by(EnvironmentCheckRecord.created_at.desc()))
        if body.mode == "local-cpu":
            if env_check is None and request.app.state.runtime.get("demo_enabled"):
                env_check = _demo_environment_check(request, db, project_id, exp, rev)
            if env_check is None:
                err("ENVIRONMENT_CHECK_REQUIRED", "environment check is required before creating a run", 409)
            if env_check.decision != "ready":
                err("ENVIRONMENT_BLOCKED", "environment is not ready for execution", 409, {"environment_check_id": env_check.id, "status": env_check.decision})
            # The sandbox recipe executes model-generated code: it may only be
            # queued when this experiment carries a reviewed model attempt whose
            # code manifest names the exact content-addressed bytes. This is a
            # creation-time gate; the worker re-verifies the bytes themselves.
            if exp.recipe_id == SANDBOX_RECIPE_ID:
                sandbox_attempt = db.scalar(
                    select(AttemptRecord).where(
                        AttemptRecord.experiment_id == exp.id,
                        AttemptRecord.source == "model",
                        AttemptRecord.run_id.is_(None),
                    ).order_by(AttemptRecord.attempt_index.desc()))
                if sandbox_attempt is None or not is_complete_sandbox_code_manifest(sandbox_attempt.code_manifest_json):
                    err("SANDBOX_CODE_REQUIRED", "the sandbox recipe requires a generated and reviewed code attempt", 409)
            # Pin the runner that will actually execute this recipe, so the
            # recorded manifest and target never claim the fixture runner for
            # a real-recipe Run.
            runner_id = (REAL_RUNNER_ID if exp.recipe_id == REAL_RECIPE_ID
                         else SANDBOX_RUNNER_ID if exp.recipe_id == SANDBOX_RECIPE_ID
                         else FIXTURE_RUNNER_ID)
            manifest = {"recipe": exp.recipe_id, "version": exp.recipe_version, "runner": runner_id, "allowlist": ["predictions"]}
            status = "queued"
            target = {"kind": "local-cpu", "runner": runner_id}
        else:
            # Remote execution has no controlled adapter yet: the Run is
            # recorded with its verified target but nothing is enqueued or
            # executed, so no local fixture output can pose as a remote run.
            env_check = db.get(EnvironmentCheckRecord, ssh_verification.environment_check_id)
            manifest = {"recipe": exp.recipe_id, "version": exp.recipe_version, "runner": None, "target": "ssh", "allowlist": []}
            status = "created"
            target = {"kind": "ssh", "ssh_verification_id": ssh_verification.id,
                      "environment_check_id": ssh_verification.environment_check_id,
                      "target": ssh_verification.target_json}
        row = RunRecord(project_id=project_id, experiment_id=exp.id, revision_id=rev.id, status=status, mode=body.mode, manifest_hash=digest(manifest), environment_check_id=env_check.id, target_json=target)
        db.add(row); db.flush()
        event = RunEventRecord(run_id=row.id, seq=0, type="run.created", payload_json={"status": status, "target": body.mode}); db.add(event)
        if status == "queued":
            JobRepository(db).enqueue(kind="run_fixture", payload_ref=row.id, idempotency_key=f"run:{row.id}")
        out = {"run_id": row.id, "status": status, "events_url": f"/api/v1/runs/{row.id}/events", "last_event_seq": 0}
        claim.record.response_json = out; receipt_save(request, db, raw, project_id, out); db.commit(); return envelope(request, out)

@router.post("/projects/{project_id}/experiments/{experiment_id}/runs", status_code=202)
async def create_run_legacy(project_id: str, experiment_id: str, request: Request, body: RunCreate = Body(...)):
    request.state._run_raw = await request.body(); return _create_run(request, project_id, experiment_id, body)

@router.post("/projects/{project_id}/runs", status_code=202)
async def create_run_root(project_id: str, request: Request, body: RunCreate = Body(...)):
    request.state._run_raw = await request.body(); return _create_run(request, project_id, None, body)

def _get_run(request: Request, run_id: str):
    db = request.app.state.runtime["session_factory"](); row = db.get(RunRecord, run_id)
    if not row: db.close(); err("RESOURCE_NOT_FOUND", "resource not found", 404)
    require_project_access(db, actor_id(request), row.project_id); return db, row

@router.get("/projects/{project_id}/runs")
def list_runs(project_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        require_project_access(db, actor_id(request), project_id)
        rows = db.scalars(select(RunRecord).where(RunRecord.project_id == project_id).order_by(RunRecord.created_at.desc())).all()
        return envelope(request, [run_json(r, db.scalar(select(RunEventRecord.seq).where(RunEventRecord.run_id == r.id).order_by(RunEventRecord.seq.desc()).limit(1)) or 0) for r in rows])

@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        last = db.scalar(
            select(RunEventRecord.seq)
            .where(RunEventRecord.run_id == run_id)
            .order_by(RunEventRecord.seq.desc())
            .limit(1)
        ) or 0
        return envelope(request, run_json(row, last))
    finally:
        db.close()


@router.post("/runs/{run_id}/repair-proposals", status_code=202)
async def create_repair_proposal_http(run_id: str, request: Request, body: RepairProposalCreate = Body(...)):
    raw = await request.body(); db, row = _get_run(request, run_id)
    try:
        write_check(request, db, row.project_id, actor_id(request))
        if row.status not in {"queued", "running", "evaluating", "failed"}:
            err("RUN_NOT_REPAIRABLE", "run is not repairable", 409)
        key = request.headers.get("idempotency-key")
        if not key: err("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"repair-proposal:{actor_id(request)}:{run_id}", key=key, body=raw)
        if claim.conflict: err("IDEMPOTENCY_CONFLICT", "idempotency key conflict", 409)
        if claim.record.response_json is not None: return envelope(request, claim.record.response_json)
        if not claim.claimed: err("IDEMPOTENCY_IN_PROGRESS", "idempotency key in progress", 409)
        exp = db.get(ExperimentRecord, row.experiment_id)
        if not exp: err("RESOURCE_NOT_FOUND", "resource not found", 404)
        try:
            proposal = create_repair_proposal(db, run_id=run_id, attempt_id=body.attempt_id,
                repair_ordinal=body.repair_ordinal, base_code_hash=body.base_code_hash,
                patch_hash=body.patch_hash, proposal_json=body.proposal_json,
                validation=body.validation, status=body.status)
            db.flush()
        except ValueError as exc:
            err("VALIDATION_ERROR", str(exc), 422)
        reserved = db.execute(update(ExperimentRecord).where(
            ExperimentRecord.id == row.experiment_id,
            ExperimentRecord.repairs_used < ExperimentRecord.repairs_limit,
        ).values(repairs_used=ExperimentRecord.repairs_used + 1)).rowcount
        if reserved != 1:
            err("REPAIR_BUDGET_EXCEEDED", "repair budget exhausted", 409)
        if proposal.status == "accepted":
            JobRepository(db).enqueue(
                kind="repair_attempt", payload_ref=proposal.id,
                idempotency_key=f"repair:{proposal.id}",
            )
        out = {"proposal_id": proposal.id, "run_id": run_id, "attempt_id": proposal.attempt_id, "status": proposal.status}
        claim.record.response_json = out; db.commit(); return envelope(request, out)
    except BaseException:
        db.rollback()
        if 'claim' in locals() and claim.claimed:
            db.execute(delete(IdempotencyKey).where(IdempotencyKey.id == claim.record.id))
            db.commit()
        raise
    finally: db.close()


@router.get("/runs/{run_id}/repair-proposals")
def get_repair_proposals_http(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        return envelope(request, [{"proposal_id": p.id, "run_id": p.run_id, "attempt_id": p.attempt_id,
                                  "repair_ordinal": p.repair_ordinal, "base_code_hash": p.base_code_hash,
                                  "patch_hash": p.patch_hash, "status": p.status,
                                  "created_at": p.created_at.isoformat()} for p in list_repair_proposals(db, run_id=run_id)])
    finally: db.close()


def _attempt_json(attempt: AttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": attempt.id,
        "run_id": attempt.run_id,
        "attempt_index": attempt.attempt_index,
        "repair_ordinal": attempt.repair_ordinal,
        "source": attempt.source,
        "status": attempt.status,
        "base_code_hash": attempt.base_code_hash,
        "patch_hash": attempt.patch_hash,
        "created_at": attempt.created_at.isoformat(),
        "updated_at": attempt.updated_at.isoformat(),
    }


def _run_attempts(db, run: RunRecord) -> list[AttemptRecord]:
    """Attempts to present for a Run: its own run-level attempts, plus — for
    sandbox runs — the experiment-level model attempt whose content-addressed
    code the sandbox actually executed (run_id NULL marks it experiment-level,
    and run.code_bound names the exact attempt id)."""
    attempts = list(db.scalars(
        select(AttemptRecord)
        .where(AttemptRecord.run_id == run.id)
        .order_by(AttemptRecord.attempt_index.asc(), AttemptRecord.created_at.asc(), AttemptRecord.id.asc())
    ).all())
    experiment = db.get(ExperimentRecord, run.experiment_id)
    if experiment is not None and experiment.recipe_id == SANDBOX_RECIPE_ID:
        attempts.extend(db.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment.id, AttemptRecord.source == "model", AttemptRecord.run_id.is_(None))
            .order_by(AttemptRecord.attempt_index.asc(), AttemptRecord.created_at.asc(), AttemptRecord.id.asc())
        ).all())
    attempts.sort(key=lambda a: (a.attempt_index, a.created_at, a.id))
    return attempts


@router.get("/runs/{run_id}/attempts")
def get_attempts_http(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        return envelope(request, [_attempt_json(attempt) for attempt in _run_attempts(db, row)])
    finally:
        db.close()


@router.get("/runs/{run_id}/curves")
def get_curves_http(
    run_id: str,
    request: Request,
    attempt_id: str | None = None,
    max_points: str = "2000",
):
    allowed_params = {"attempt_id", "max_points"}
    unknown_params = sorted(set(request.query_params) - allowed_params)
    if unknown_params:
        err("VALIDATION_ERROR", "unknown query parameter", 422, {"parameters": unknown_params})
    try:
        parsed_max_points = int(max_points)
    except (TypeError, ValueError):
        err("VALIDATION_ERROR", "max_points must be an integer", 422)
    if parsed_max_points < 1 or parsed_max_points > 2000:
        err("VALIDATION_ERROR", "max_points must be between 1 and 2000", 422)

    db, row = _get_run(request, run_id)
    try:
        if attempt_id is not None:
            attempt = db.scalar(
                select(AttemptRecord).where(
                    AttemptRecord.id == attempt_id,
                    AttemptRecord.run_id == run_id,
                )
            )
            if attempt is None:
                err("RESOURCE_NOT_FOUND", "resource not found", 404)
        else:
            attempt = db.scalars(
                select(AttemptRecord)
                .where(AttemptRecord.run_id == run_id)
                .order_by(
                    AttemptRecord.attempt_index.desc(),
                    AttemptRecord.created_at.desc(),
                    AttemptRecord.id.desc(),
                )
            ).first()

        return envelope(request, {
            "run_id": row.id,
            "attempt_id": attempt.id if attempt else None,
            "series": [],
            "downsampled": False,
            "warnings": ["过程曲线尚未持久化，当前无可用曲线数据"],
        })
    finally:
        db.close()


@router.get("/runs/{run_id}/executions")
def get_executions_http(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        executions = db.scalars(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id).order_by(ExecutionRecord.created_at.asc(), ExecutionRecord.id.asc())).all()
        return envelope(request, [{
            "execution_id": execution.id,
            "run_id": execution.run_id,
            "attempt_id": execution.attempt_id,
            "job_id": execution.job_id,
            "status": execution.status,
            "handle": execution.handle,
            "started_at": execution.started_at.isoformat() if execution.started_at else None,
            "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
            "error": execution.error_json,
            "created_at": execution.created_at.isoformat(),
        } for execution in executions])
    finally:
        db.close()


def _safe_hash(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def _safe_execution_error(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    safe = {
        key: value[key]
        for key in ("code", "message")
        if isinstance(value.get(key), str)
    }
    return safe or None


def _manifest_execution_json(execution: ExecutionRecord) -> dict[str, Any]:
    return {
        "execution_id": execution.id,
        "run_id": execution.run_id,
        "attempt_id": execution.attempt_id,
        "job_id": execution.job_id,
        "status": execution.status,
        "handle": execution.handle,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        "error": _safe_execution_error(execution.error_json),
        "created_at": execution.created_at.isoformat(),
    }


def _manifest_export_json(export: ExportRecord) -> dict[str, Any]:
    valid_artifact = (
        export.status == "succeeded"
        and _safe_hash(export.artifact_id) is not None
        and _safe_hash(export.sha256) == _safe_hash(export.artifact_id)
    )
    artifact_id = export.artifact_id if valid_artifact else None
    items = []
    for item in (export.items_json or []):
        if not isinstance(item, dict):
            continue
        item_artifact_id = item.get("artifact_id") if valid_artifact and _safe_hash(item.get("artifact_id")) == artifact_id else None
        items.append({
            "format": item["format"] if isinstance(item.get("format"), str) else None,
            "status": item["status"] if isinstance(item.get("status"), str) else None,
            "artifact_id": item_artifact_id,
        })
    return {"id": export.id, "run_id": export.run_id, "status": export.status, "items": items, "artifact_id": artifact_id, "bytes": export.bytes if valid_artifact else None, "created_at": export.created_at.isoformat()}


@router.get("/runs/{run_id}/manifest")
def get_run_manifest(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        attempts = _run_attempts(db, row)
        executions = db.scalars(
            select(ExecutionRecord)
            .where(ExecutionRecord.run_id == run_id)
            .order_by(ExecutionRecord.created_at.asc(), ExecutionRecord.id.asc())
        ).all()
        exports = db.scalars(
            select(ExportRecord)
            .where(ExportRecord.run_id == run_id)
            .order_by(ExportRecord.created_at.asc(), ExportRecord.id.asc())
        ).all()
        return envelope(request, {
            "run_id": row.id,
            # No persisted finalized marker exists yet, so this endpoint must
            # not imply that the complete run evidence has been finalized.
            "status": "provisional",
            "manifest_hash": _safe_hash(row.manifest_hash),
            "metrics_hash": _safe_hash(row.metrics_hash),
            "report_hash": _safe_hash(row.report_hash),
            "attempts": [_attempt_json(attempt) for attempt in attempts],
            "executions": [_manifest_execution_json(execution) for execution in executions],
            "exports": [_manifest_export_json(export) for export in exports],
        })
    finally:
        db.close()


def _get_execution_control(request: Request, execution_id: str):
    db = request.app.state.runtime["session_factory"]()
    execution = db.get(ExecutionRecord, execution_id)
    if execution is None:
        db.close()
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    run = db.get(RunRecord, execution.run_id)
    if run is None:
        db.close()
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    require_project_access(db, actor_id(request), run.project_id, action="execute")
    return db, execution


@router.post("/executions/{execution_id}/heartbeat")
def heartbeat_execution(execution_id: str, request: Request, body: ExecutionHeartbeat = Body(...)):
    db, execution = _get_execution_control(request, execution_id)
    try:
        updated = ExecutionRepository(db).heartbeat(
            execution_id,
            runner_id=body.runner_id,
            fencing_token=body.fencing_token,
            lease_seconds=body.lease_seconds,
        )
        if not updated:
            db.rollback()
            err("EXECUTION_FENCE_REJECTED", "execution owner or fencing token is stale", 409)
        db.commit()
        return envelope(request, {
            "execution_id": execution_id,
            "status": "running",
            "lease_seconds": body.lease_seconds,
        })
    finally:
        db.close()


@router.post("/executions/{execution_id}/finish")
def finish_execution(execution_id: str, request: Request, body: ExecutionFinish = Body(...)):
    db, execution = _get_execution_control(request, execution_id)
    try:
        run = db.get(RunRecord, execution.run_id)
        if run is not None and run.status == "cancel_requested":
            cancelled = ExecutionRepository(db).cancel(
                execution_id,
                runner_id=body.runner_id,
                fencing_token=body.fencing_token,
            )
            if cancelled:
                run.status = "cancelled"
                latest = db.scalar(select(RunEventRecord.seq).where(RunEventRecord.run_id == run.id).order_by(RunEventRecord.seq.desc()).limit(1))
                db.add(RunEventRecord(run_id=run.id, seq=(latest if latest is not None else -1) + 1, type="run.cancelled", payload_json={"status": "cancelled"}))
                db.commit()
            else:
                db.rollback()
            err("EXECUTION_CANCELLED", "execution cancellation was requested", 409)
        updated = ExecutionRepository(db).finish(
            execution_id,
            runner_id=body.runner_id,
            fencing_token=body.fencing_token,
            status=body.status,
            error_json=body.error,
        )
        if not updated:
            db.rollback()
            err("EXECUTION_FENCE_REJECTED", "execution owner or fencing token is stale", 409)
        db.commit()
        return envelope(request, {
            "execution_id": execution_id,
            "status": body.status,
            "error": body.error,
        })
    finally:
        db.close()


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, request: Request, after: int = 0):
    if after < 0: err("VALIDATION_ERROR", "after must be non-negative")
    db, row = _get_run(request, run_id)
    try:
        earliest = db.scalar(select(RunEventRecord.seq).where(RunEventRecord.run_id == run_id).order_by(RunEventRecord.seq).limit(1))
        if earliest is not None and after < earliest - 1: err("EVENT_CURSOR_EXPIRED", "event cursor expired", 409)
        rows = db.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.seq > after).order_by(RunEventRecord.seq)).all()
        def stream():
            for event in rows:
                data = {"schema_version": "1.0", "run_id": run_id, "seq": event.seq, "type": event.type, "occurred_at": event.created_at.isoformat(), "payload": event.payload_json}
                yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")
    finally: db.close()

@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, request: Request, body: RunCancel = Body(default=RunCancel())):
    raw = await request.body(); db, row = _get_run(request, run_id)
    try:
        write_check(request, db, row.project_id, actor_id(request)); key = request.headers.get("idempotency-key")
        cached = receipt_lookup(request, db, raw)
        if cached is not None: return envelope(request, cached)
        if not key: err("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"cancel:{actor_id(request)}:{run_id}", key=key, body=raw)
        if claim.conflict: err("IDEMPOTENCY_CONFLICT", "idempotency key conflict", 409)
        if claim.record.response_json is not None: return envelope(request, claim.record.response_json)
        if not claim.claimed: err("IDEMPOTENCY_IN_PROGRESS", "idempotency key in progress", 409)
        if row.status not in {"completed", "failed", "cancelled", "awaiting_revision", "cancel_requested"}:
            row.status = "cancel_requested"
            latest = db.scalar(select(RunEventRecord.seq).where(RunEventRecord.run_id == run_id).order_by(RunEventRecord.seq.desc()).limit(1))
            seq = (latest if latest is not None else -1) + 1
            db.add(RunEventRecord(run_id=run_id, seq=seq, type="run.cancel_requested", payload_json={"reason": body.reason}))
        out = run_json(row); claim.record.response_json = out; receipt_save(request, db, raw, row.project_id, out); db.commit(); return envelope(request, out)
    finally: db.close()

@router.get("/runs/{run_id}/report")
def get_report(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try:
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == row.id))
        if not report: err("REPORT_NOT_READY", "report is not ready", 409)
        return envelope(request, report.content_json)
    finally: db.close()

def _export_json(x: ExportRecord, db=None) -> dict[str, Any]:
    items = [{key: item[key] for key in ("format", "status", "artifact_id", "error") if key in item} for item in (x.items_json or []) if isinstance(item, dict)]
    if db is not None:
        registered = db.scalars(select(ExportItemRecord).where(
            ExportItemRecord.export_id == x.id).order_by(ExportItemRecord.format)).all()
        if registered:
            items = [{"format": item.format, "status": item.status,
                      "artifact_id": item.artifact_id, "sha256": item.sha256,
                      "bytes": item.bytes, "media_type": item.media_type,
                      "filename": FILENAMES.get(item.format),
                      **({"error": item.error_json} if item.error_json else {})}
                     for item in registered]
    return {"id": x.id, "run_id": x.run_id, "status": x.status, "items": items, "artifact_id": x.artifact_id, "bytes": x.bytes, "report_hash": x.report_hash, "operation_id": x.operation_id, "created_at": x.created_at.isoformat()}


@router.get("/runs/{run_id}/exports")
def list_exports(run_id: str, request: Request):
    db, row = _get_run(request, run_id)
    try: return envelope(request, [_export_json(x, db) for x in db.scalars(select(ExportRecord).where(ExportRecord.run_id == run_id).order_by(ExportRecord.created_at.desc())).all()])
    finally: db.close()


def _safe_manifest_path(path: Any) -> str | None:
    if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return None
    normalized = path.replace("\\", "/")
    from pathlib import PurePosixPath
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or ".." in parsed.parts:
        return None
    return "/".join(parsed.parts)


def _manifest_artifact(db, artifact_id: str):
    rows = db.scalars(select(AttemptRecord)).all()
    for attempt in rows:
        manifest = attempt.code_manifest_json
        entries = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
                run = db.get(RunRecord, attempt.run_id)
                if run is not None:
                    return attempt, run, item
    return None


@router.get("/attempts/{attempt_id}/files")
def get_attempt_files(attempt_id: str, request: Request, limit: str = "20", cursor: str | None = None):
    """Return only manifest entries whose content-addressed bytes verify."""
    allowed_params = {"limit", "cursor"}
    unknown_params = sorted(set(request.query_params) - allowed_params)
    if unknown_params:
        err("VALIDATION_ERROR", "unknown query parameter", 422, {"parameters": unknown_params})
    try:
        parsed_limit = int(limit)
    except (TypeError, ValueError):
        err("VALIDATION_ERROR", "limit must be an integer", 422)
    if parsed_limit < 1 or parsed_limit > 100:
        err("VALIDATION_ERROR", "limit must be between 1 and 100", 422)
    db = request.app.state.runtime["session_factory"]()
    try:
        attempt = db.get(AttemptRecord, attempt_id)
        if attempt is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        run = db.get(RunRecord, attempt.run_id)
        if run is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        require_project_access(db, actor_id(request), run.project_id)
        store = request.app.state.runtime.get("artifact_store")
        entries = (attempt.code_manifest_json or {}).get("files") if isinstance(attempt.code_manifest_json, dict) else None
        files: list[dict[str, Any]] = []
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict):
                continue
            path = _safe_manifest_path(item.get("path")); artifact_id = item.get("artifact_id")
            media_type = item.get("media_type"); registered_bytes = item.get("bytes"); registered_sha256 = item.get("sha256")
            if (path is None or _safe_hash(artifact_id) is None or not isinstance(media_type, str)
                    or not isinstance(registered_bytes, int) or isinstance(registered_bytes, bool) or registered_bytes < 0
                    or _safe_hash(registered_sha256) != artifact_id or store is None):
                continue
            try:
                content = store.open_bytes(artifact_id)
            except (ArtifactNotFoundError, ArtifactIntegrityError):
                continue
            if len(content) != registered_bytes:
                continue
            files.append({"path": path, "artifact_id": artifact_id, "bytes": registered_bytes,
                          "sha256": registered_sha256, "media_type": media_type})
        files.sort(key=lambda item: (item["path"], item["artifact_id"]))
        start = 0
        if cursor is not None:
            try: start = int(cursor)
            except (TypeError, ValueError): err("VALIDATION_ERROR", "cursor is invalid", 422)
            if start < 0: err("VALIDATION_ERROR", "cursor is invalid", 422)
        page = files[start:start + parsed_limit]
        return envelope(request, page) | {"page": {"has_more": start + len(page) < len(files),
                                                     "next_cursor": str(start + len(page)) if start + len(page) < len(files) else None}}
    finally:
        db.close()


_TEXT_MEDIA_TYPES = {"text/plain", "text/markdown", "text/csv", "text/x-python", "text/x-script.python", "application/json"}


def _evidence_artifact(db, artifact_id: str):
    """Resolve an artifact id to the Run whose registered evidence carries it.

    Evidence entries live on the run's event log (see evidence_registry), so a
    run's real output is readable before any export registers it. Evidence
    events are one per run, so the exact sha match happens in Python.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
        return None
    events = db.scalars(select(RunEventRecord).where(
        RunEventRecord.type == EVIDENCE_EVENT_TYPE).order_by(RunEventRecord.run_id)).all()
    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else None
        entries = payload.get("evidence") if payload else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("sha256") == artifact_id:
                run = db.get(RunRecord, event.run_id)
                if run is not None:
                    return run, entry
    return None


def _registered_artifact(request: Request, db, artifact_id: str):
    # The same content hash can be registered under several Runs. Evaluate
    # every registration and keep the first one this actor may read, so an
    # inaccessible earlier row can never deny a legitimate later one.
    candidates: list[tuple[Any, RunRecord, ExportRecord | None]] = []
    for item in db.scalars(select(ExportItemRecord).where(
            ExportItemRecord.artifact_id == artifact_id, ExportItemRecord.status == "ready")).all():
        export = db.get(ExportRecord, item.export_id)
        run = db.get(RunRecord, export.run_id) if export else None
        if export is None or run is None:
            continue
        candidates.append((item, run, export))
    for export in db.scalars(select(ExportRecord).where(
            ExportRecord.artifact_id == artifact_id, ExportRecord.status.in_(["succeeded", "partial"]))).all():
        run = db.get(RunRecord, export.run_id)
        if run is None:
            continue
        if any(existing[2] is export for existing in candidates):
            continue
        candidates.append((export, run, export))
    found = _manifest_artifact(db, artifact_id)
    if found is not None:
        _, run, manifest_item = found
        candidates.append((manifest_item, run, None))
    evidence = _evidence_artifact(db, artifact_id)
    if evidence is not None:
        run, entry = evidence
        candidates.append(({"sha256": entry["sha256"], "bytes": entry["bytes"],
                            "media_type": entry["media_type"], "filename": "evidence.json"}, run, None))
    if not candidates:
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    actor = actor_id(request)
    chosen = None
    for metadata, run, export in candidates:
        try:
            require_project_access(db, actor, run.project_id)
        except ServiceError as exc:
            if getattr(exc, "status", None) in (401, 403, 404):
                continue
            raise
        chosen = (metadata, run, export)
        break
    if chosen is None:
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    metadata, run, export = chosen
    registered_sha = metadata.get("sha256") if isinstance(metadata, dict) else metadata.sha256
    registered_bytes = metadata.get("bytes") if isinstance(metadata, dict) else metadata.bytes
    media_type = metadata.get("media_type") if isinstance(metadata, dict) else metadata.media_type
    if (_safe_hash(registered_sha) != artifact_id or not isinstance(registered_bytes, int) or isinstance(registered_bytes, bool) or registered_bytes < 0 or not isinstance(media_type, str)):
        err("ARTIFACT_INTEGRITY_FAILED", "artifact metadata hash mismatch", 409)
    if isinstance(metadata, ExportItemRecord) and (
            metadata.format not in MEDIA or media_type != MEDIA[metadata.format]):
        err("ARTIFACT_INTEGRITY_FAILED", "artifact media type mismatch", 409)
    if isinstance(metadata, ExportRecord) and media_type != "application/json":
        err("ARTIFACT_INTEGRITY_FAILED", "artifact media type mismatch", 409)
    if export is not None and export.artifact_id == artifact_id and (
            export.sha256 != artifact_id or export.bytes != registered_bytes or export.media_type != media_type):
        err("ARTIFACT_INTEGRITY_FAILED", "artifact metadata hash mismatch", 409)
    store = request.app.state.runtime.get("artifact_store")
    if store is None: err("ARTIFACT_UNAVAILABLE", "artifact store unavailable", 503)
    try: content = store.open_bytes(artifact_id)
    except ArtifactNotFoundError: err("ARTIFACT_NOT_FOUND", "artifact not found", 404)
    except ArtifactIntegrityError: err("ARTIFACT_INTEGRITY_FAILED", "artifact integrity check failed", 409)
    if len(content) != registered_bytes or hashlib.sha256(content).hexdigest() != artifact_id:
        err("ARTIFACT_INTEGRITY_FAILED", "artifact byte count mismatch", 409)
    return metadata, content, media_type


@router.get("/artifacts/{artifact_id}/text")
def read_artifact_text(artifact_id: str, request: Request, offset: str = "0", limit: str = "65536"):
    allowed_params = {"offset", "limit"}
    unknown_params = sorted(set(request.query_params) - allowed_params)
    if unknown_params:
        err("VALIDATION_ERROR", "unknown query parameter", 422, {"parameters": unknown_params})
    try:
        parsed_offset = int(offset); parsed_limit = int(limit)
    except (TypeError, ValueError):
        err("VALIDATION_ERROR", "offset and limit must be integers", 422)
    if parsed_offset < 0:
        err("VALIDATION_ERROR", "offset must be non-negative", 422)
    if parsed_limit < 1 or parsed_limit > 65536:
        err("VALIDATION_ERROR", "limit must be between 1 and 65536", 422)
    db = request.app.state.runtime["session_factory"]()
    try:
        metadata, content, metadata_media_type = _registered_artifact(request, db, artifact_id)
        media_type = (metadata_media_type or "").split(";", 1)[0].strip().lower()
        if media_type not in _TEXT_MEDIA_TYPES:
            err("ARTIFACT_NOT_TEXT", "artifact is not a supported text media type", 422)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            err("ARTIFACT_NOT_UTF8", "artifact is not valid UTF-8", 422)
        if parsed_offset > len(content):
            err("VALIDATION_ERROR", "offset is beyond end of artifact", 422)
        requested_offset = parsed_offset
        if parsed_offset:
            try:
                content[:parsed_offset].decode("utf-8")
            except UnicodeDecodeError:
                # Preserve the historical contract for offsets inside the
                # first code point, while making later cursors resumable by
                # advancing to the next valid UTF-8 boundary.
                prefix_boundary = parsed_offset
                while prefix_boundary > 0:
                    prefix_boundary -= 1
                    try:
                        content[:prefix_boundary].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        continue
                if prefix_boundary == 0:
                    err("VALIDATION_ERROR", "offset is not a UTF-8 boundary", 422)
                aligned = parsed_offset
                while aligned < len(content):
                    aligned += 1
                    try:
                        content[:aligned].decode("utf-8")
                        parsed_offset = aligned
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    err("VALIDATION_ERROR", "offset is not a UTF-8 boundary", 422)
                # A cursor that was supplied inside a code point is commonly
                # produced by line-oriented previews. Do not return the
                # separator that belongs to the preceding line after aligning.
                if parsed_offset < len(content) and content[parsed_offset:parsed_offset + 1] in {b"\n", b"\r"}:
                    parsed_offset += 1

        end = min(parsed_offset + parsed_limit, len(content))
        while end > parsed_offset:
            try:
                text = content[parsed_offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            text = ""
        return envelope(request, {"artifact_id": artifact_id, "text": text, "offset": parsed_offset,
                                  "requested_offset": requested_offset,
                                  "next_offset": end, "eof": end >= len(content)})
    finally:
        db.close()


@router.get("/artifacts/{artifact_id}/raw")
def read_artifact_raw(artifact_id: str, request: Request):
    from p2r_orchestration.autonomous.evidence import MAX_FILE
    from p2r_orchestration.autonomous.files import raw_headers
    unknown_params = sorted(set(request.query_params))
    if unknown_params:
        err("VALIDATION_ERROR", "unknown query parameter", 422, {"parameters": unknown_params})
    db = request.app.state.runtime["session_factory"]()
    try:
        metadata, content, media_type = _registered_artifact(request, db, artifact_id)
        if len(content) > MAX_FILE:
            err("FILE_TOO_LARGE", "artifact exceeds preview size limit", 413)
        filename = metadata.get("filename") if isinstance(metadata, dict) else getattr(metadata, "filename", None)
        if not isinstance(filename, str) or not filename:
            filename = metadata.get("path") if isinstance(metadata, dict) else None
        if not isinstance(filename, str) or not filename:
            filename = "artifact-" + artifact_id[:12]
        content_type, headers = raw_headers(media_type, content, filename)
        return Response(content=content, headers={**headers, "Content-Type": content_type})
    finally:
        db.close()


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, request: Request):
    db = request.app.state.runtime["session_factory"]()
    try:
        metadata, content, media_type = _registered_artifact(request, db, artifact_id)
        # Content-Disposition only ever uses the fixed ASCII whitelist keyed by
        # format/media type; user or DB text is never copied into the header.
        allowed_names = set(FILENAMES.values()) | {"evidence.json"}
        filename = metadata.get("filename") if isinstance(metadata, dict) else getattr(metadata, "filename", None)
        if filename not in allowed_names:
            filename = next((FILENAMES[fmt] for fmt, mt in MEDIA.items() if mt == media_type), None)
        if filename not in allowed_names:
            filename = "artifact.bin"
        return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(len(content))})
    finally: db.close()

@router.post("/runs/{run_id}/exports", status_code=202)
async def create_export(run_id: str, request: Request, body: ExportCreate = Body(...)):
    raw = await request.body(); db, row = _get_run(request, run_id)
    try:
        write_check(request, db, row.project_id, actor_id(request)); formats = list(body.formats)
        if body.include_checkpoint: err("UNSUPPORTED_FORMAT", "checkpoint export is unavailable", 422)
        cached = receipt_lookup(request, db, raw)
        if cached is not None: return envelope(request, cached)
        allowed = {"report_json", "report_markdown", "bundle_zip", "report_pdf"}
        if any(x not in allowed for x in formats): err("UNSUPPORTED_FORMAT", "unsupported export format", 422)
        if not formats or len(formats) != len(set(formats)): err("UNSUPPORTED_FORMAT", "at least one unique format is required", 422)
        # The report hash is frozen at request time when a report exists. A
        # missing report still queues the export (historical contract) and the
        # worker fails it with a fixed safe code instead of guessing a hash.
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
        report_hash = report.content_hash if report else None
        key = request.headers.get("idempotency-key")
        if not key: err("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"export:{actor_id(request)}:{run_id}", key=key, body=raw)
        if claim.conflict: err("IDEMPOTENCY_CONFLICT", "idempotency key conflict", 409)
        if claim.record.response_json is not None: return envelope(request, claim.record.response_json)
        if not claim.claimed: err("IDEMPOTENCY_IN_PROGRESS", "idempotency key in progress", 409)
        ex = ExportRecord(run_id=run_id, report_hash=report_hash, requested_formats_json=formats, items_json=[]); db.add(ex); db.flush()
        for fmt in formats: db.add(ExportItemRecord(export_id=ex.id, format=fmt, status="pending"))
        job_kind = "export_report_json" if formats == ["report_json"] else "export_bundle"
        operation = OperationRecord(kind=job_kind, resource_id=ex.id, status="queued")
        db.add(operation); db.flush()
        ex.operation_id = operation.id
        JobRepository(db).enqueue(kind=job_kind, payload_ref=ex.id, idempotency_key=f"export:{ex.id}")
        out = {"export_id": ex.id, "operation_id": operation.id, "status": "queued"}; claim.record.response_json = out; receipt_save(request, db, raw, row.project_id, out); db.commit(); return envelope(request, out)
    finally: db.close()
