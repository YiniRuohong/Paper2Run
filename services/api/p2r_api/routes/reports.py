from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import APIRouter, Request
from sqlalchemy import select, update

from p2r_artifacts.store import ArtifactStoreError
from p2r_orchestration.evidence_registry import read_registered_entries
from p2r_orchestration.report_builder import EVALUATION_EVENT_TYPE, LIMITATIONS, REAL_LIMITATIONS, SANDBOX_LIMITATIONS, build_report, project_confidence_intervals, project_metrics, report_hash
from p2r_orchestration.service import ServiceError
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    EnvironmentCheckRecord, EvaluationRecord, ExperimentRecord, ExtractionRecord,
    OperationRecord, PaperRecord, ReportBuildRecord, ReportRecord, RunEventRecord, RunRecord,
    SpecRevisionRecord,
)
from p2r_persistence.repositories import IdempotencyRepository
from p2r_runner import FIXTURE_RECIPE_ID, FIXTURE_RECIPE_VERSION, REAL_RECIPE_ID, REAL_RECIPE_VERSION, SANDBOX_RECIPE_ID, SANDBOX_RECIPE_VERSION
from ..auth import require_project_access
from ..demo_receipts import lookup as receipt_lookup, save as receipt_save
from ..dependencies import actor_id
from ..paper_text import latest_paper_text
from .experiments import write_check

router = APIRouter()


def _err(code, message, status=422):
    error = ServiceError(message)
    error.code, error.status = code, status
    raise error


def _env(request, data):
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def _authorized_run(db, request, run_id):
    run = db.get(RunRecord, run_id)
    if run is None:
        _err("RESOURCE_NOT_FOUND", "resource not found", 404)
    require_project_access(db, actor_id(request), run.project_id)
    return run


def _target_snapshot(run: RunRecord) -> dict[str, Any]:
    """The execution target pinned when the Run was created.

    Rows that predate target pinning: only the local fixture target is
    reconstructible from the mode; anything else stays explicitly unknown
    instead of being guessed.
    """
    if isinstance(run.target_json, dict) and run.target_json:
        return dict(run.target_json)
    if run.mode == "local-cpu":
        return {"kind": "local-cpu", "runner": "local-fixture-v1"}
    return {"kind": run.mode, "runner": None}


def _environment_snapshot(db, run: RunRecord) -> dict[str, Any]:
    """The environment decision this Run was actually gated by.

    Runs created through the gated route pin their environment check; a
    pinned record that no longer matches the Run is corruption and fails the
    build. Legacy rows fall back to the latest recorded check for the same
    revision, or an explicit ``recorded: False`` marker — never a fabricated
    decision.
    """
    if run.environment_check_id:
        record = db.get(EnvironmentCheckRecord, run.environment_check_id)
        if (record is None or record.revision_id != run.revision_id
                or record.experiment_id != run.experiment_id):
            raise ValueError("REPORT_INPUT_INVALID")
    else:
        record = db.scalar(
            select(EnvironmentCheckRecord)
            .where(EnvironmentCheckRecord.revision_id == run.revision_id,
                   EnvironmentCheckRecord.experiment_id == run.experiment_id)
            .order_by(EnvironmentCheckRecord.created_at.desc())
        )
    if record is None:
        return {"recorded": False, "decision": None, "environment_check_id": None}
    return {
        "recorded": True,
        "environment_check_id": record.id,
        "decision": record.decision,
        "checker_version": record.checker_version,
        "items": record.items_json,
        "created_at": record.created_at.isoformat(),
    }


def _source_block(db, paper, extraction) -> dict:
    """Frozen source identity; PDF papers also record which backend parsed them and what was missing."""
    source = {"paper_id": paper.id, "extraction_id": extraction.id, "sha256": paper.sha256, "bytes": paper.bytes}
    text = latest_paper_text(db, paper, status="ready")
    if text is not None and text.backend:
        source["parse"] = {"backend": text.backend, "text_sha256": text.text_sha256,
                           "pages_parsed": text.pages_parsed, "page_count": text.page_count,
                           "warnings": list(text.warnings_json or [])}
    return source


def _snapshot(db, request, run):
    """Validate registered inputs before projecting a bounded immutable snapshot."""
    revision = db.get(SpecRevisionRecord, run.revision_id)
    experiment = db.get(ExperimentRecord, run.experiment_id)
    evaluation = db.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run.id))
    if (revision is None or experiment is None or evaluation is None
            or revision.experiment_id != experiment.id or experiment.project_id != run.project_id
            or report_hash(revision.spec_json) != revision.spec_hash):
        raise ValueError("REPORT_INPUT_INVALID")
    metrics = project_metrics(evaluation.metrics_json)
    if report_hash(metrics) != evaluation.metrics_hash or evaluation.metrics_hash != run.metrics_hash:
        raise ValueError("REPORT_INPUT_INVALID")
    paper = db.get(PaperRecord, experiment.paper_id)
    extraction = db.get(ExtractionRecord, experiment.extraction_id)
    store = request.app.state.runtime.get("artifact_store")
    if (paper is None or extraction is None or store is None
            or paper.project_id != run.project_id or extraction.paper_id != paper.id
            or extraction.status != "ready" or extraction.content_sha256 != paper.sha256):
        raise ValueError("REPORT_INPUT_INVALID")
    # The store is content-addressed: the lowercase sha256 is the address.
    # (artifact_key may hold a filesystem path from some upload routes.)
    source = store.open_bytes(paper.sha256)
    if len(source) != paper.bytes or hashlib.sha256(source).hexdigest() != paper.sha256:
        raise ValueError("REPORT_INPUT_INVALID")
    evidence = read_registered_entries(db, run.id, store)
    target = _target_snapshot(run)
    environment = _environment_snapshot(db, run)
    controlled = (experiment.recipe_id == FIXTURE_RECIPE_ID
                  and experiment.recipe_version == FIXTURE_RECIPE_VERSION
                  and target.get("kind") == "local-cpu")
    real_local_cpu = (experiment.recipe_id == REAL_RECIPE_ID
                      and experiment.recipe_version == REAL_RECIPE_VERSION
                      and target.get("kind") == "local-cpu")
    sandboxed_model_code = (experiment.recipe_id == SANDBOX_RECIPE_ID
                            and experiment.recipe_version == SANDBOX_RECIPE_VERSION
                            and target.get("kind") == "local-cpu")
    snapshot = {
        "run_id": run.id,
        "revision_id": revision.id,
        "recipe": {"id": experiment.recipe_id, "version": experiment.recipe_version},
        "metrics": metrics,
        "metrics_hash": evaluation.metrics_hash,
        "evaluator_version": evaluation.evaluator_version,
        "manifest": {"status": "provisional", "manifest_hash": run.manifest_hash},
        # The confirmed execution target and the environment decision this Run
        # was actually gated by, both pinned at creation time.
        "target": target,
        "environment": environment,
        # Only this Run's registered evidence, re-verified against the store at
        # freeze time. Runs without a registration legitimately freeze nothing;
        # their ZIP export then honestly fails with EXPORT_INCOMPLETE.
        "evidence": evidence,
        "run_created_at": run.created_at.isoformat(),
        "revision": {"id": revision.id, "spec_hash": revision.spec_hash, "created_at": revision.created_at.isoformat()},
        "source": _source_block(db, paper, extraction),
        "limitations": list(
            SANDBOX_LIMITATIONS if sandboxed_model_code
            else REAL_LIMITATIONS if real_local_cpu
            else LIMITATIONS
        ),
        # Explicit truth marking: the controlled local smoke fixture, a real
        # local-CPU recipe run, and a sandboxed model-code run carry different
        # evidence classes; none is ever presented as a real paper
        # reproduction.
        "evidence_class": (
            "sandboxed_model_code" if sandboxed_model_code
            else "real_local_cpu" if real_local_cpu
            else "controlled_smoke" if controlled
            else "unclassified"
        ),
        "controlled_smoke": bool(controlled),
    }
    # Legacy runs have no evaluator event. Do not infer CI from runner evidence
    # or fabricate it; a present registration must match this evaluation.
    events = db.scalars(select(RunEventRecord).where(
        RunEventRecord.run_id == run.id, RunEventRecord.type == EVALUATION_EVENT_TYPE,
    )).all()
    if events:
        if len(events) != 1:
            raise ValueError("REPORT_INPUT_INVALID")
        payload = events[0].payload_json
        if (not isinstance(payload, dict)
                or set(payload) != {"metrics_hash", "confidence_intervals"}
                or payload["metrics_hash"] != evaluation.metrics_hash):
            raise ValueError("REPORT_INPUT_INVALID")
        snapshot["confidence_intervals"] = project_confidence_intervals(payload["confidence_intervals"])
    build_report(snapshot)
    return snapshot


@router.get("/runs/{run_id}/report-status")
def report_status(run_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        run = _authorized_run(db, request, run_id)
        build = db.get(ReportBuildRecord, run.active_report_build_id) if run.active_report_build_id else None
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
        return _env(request, {
            "run_id": run_id,
            "status": "ready" if report else (build.status if build else None),
            "build_id": build.id if build else None,
            "operation_id": build.operation_id if build else None,
            "report_hash": report.content_hash if report else None,
            "error": {"code": "REPORT_BUILD_FAILED"} if build and build.status == "failed" and not report else None,
        })


@router.post("/runs/{run_id}/report-builds", status_code=202)
async def create_report_build(run_id: str, request: Request):
    raw = await request.body()
    with request.app.state.runtime["session_factory"]() as db:
        # SQLite's legacy SELECT/SAVEPOINT mode otherwise permits partial commits.
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        run = _authorized_run(db, request, run_id)
        user_id = actor_id(request)
        write_check(request, db, run.project_id, user_id)
        key = request.headers.get("idempotency-key")
        if not key:
            _err("VALIDATION_ERROR", "Idempotency-Key is required")
        cached = receipt_lookup(request, db, raw)
        if cached is not None:
            return _env(request, cached)
        claim = IdempotencyRepository(db).claim(scope=f"report-build:{user_id}:{run_id}", key=key, body=raw)
        if claim.conflict:
            _err("IDEMPOTENCY_CONFLICT", "idempotency key conflict", 409)
        if claim.record.response_json is not None:
            return _env(request, claim.record.response_json)
        if not claim.claimed:
            _err("IDEMPOTENCY_IN_PROGRESS", "idempotency key in progress", 409)
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, UnicodeError):
            _err("VALIDATION_ERROR", "invalid JSON")
        if (not isinstance(payload, dict) or set(payload) - {"retry_of"}
                or ("retry_of" in payload and (not isinstance(payload["retry_of"], str) or not payload["retry_of"]))):
            _err("VALIDATION_ERROR", "invalid report build body")
        retry_of = payload.get("retry_of")
        current = db.get(ReportBuildRecord, run.active_report_build_id) if run.active_report_build_id else None
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))

        def respond(build, status):
            out = {"build_id": build.id if build else None, "operation_id": build.operation_id if build else None,
                   "run_id": run_id, "status": status}
            claim.record.response_json = out
            receipt_save(request, db, raw, run.project_id, out)
            db.commit()
            return _env(request, out)

        if report is not None:
            if retry_of is not None:
                _err("REPORT_RETRY_INVALID", "ready reports cannot be retried", 409)
            return respond(current, "ready")
        if run.status != "completed":
            _err("RUN_NOT_COMPLETED", "run must be completed", 409)
        if retry_of is not None:
            if current is None or current.id != retry_of or current.run_id != run_id or current.status != "failed":
                _err("REPORT_RETRY_INVALID", "only the current failed build may be retried", 409)
        elif current is not None:
            if current.status in {"pending", "generating"}:
                return respond(current, current.status)
            _err("REPORT_RETRY_INVALID", "an explicit retry of the current failed build is required", 409)
        try:
            snapshot = _snapshot(db, request, run)
        except (ValueError, TypeError, OSError, ArtifactStoreError):
            _err("REPORT_INPUT_INVALID", "report inputs unavailable", 409)
        # Reserve the slot before inserting generation rows, avoiding uniqueness
        # races. All dependent records and the receipt commit with this CAS.
        build_id = "rb_" + str(uuid.uuid4())
        active = run.active_report_build_id
        reserved = db.execute(update(RunRecord).where(
            RunRecord.id == run_id, RunRecord.active_report_build_id == active,
            RunRecord.status == "completed", RunRecord.report_hash.is_(None),
        ).values(active_report_build_id=build_id))
        if reserved.rowcount != 1:
            _err("REPORT_BUILD_CONFLICT", "report build changed; reload before retrying", 409)
        operation = OperationRecord(kind="report_build", resource_id=run_id, status="queued")
        db.add(operation)
        db.flush()
        build = ReportBuildRecord(
            id=build_id, run_id=run_id, operation_id=operation.id,
            generation=current.generation + 1 if current else 1, status="pending",
            input_json=snapshot, input_hash=report_hash(snapshot),
        )
        db.add(build)
        db.flush()
        JobRepository(db).enqueue(kind="report_build", payload_ref=build.id, idempotency_key=f"report-build:{build.id}")
        return respond(build, "pending")
