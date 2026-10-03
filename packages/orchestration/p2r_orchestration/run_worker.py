from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from p2r_orchestration.export_jobs import process_export_job
from sqlalchemy import select
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import AttemptRecord, EvaluationRecord, ExecutionRecord, ExportRecord, RepairProposalRecord, ReportRecord, RunEventRecord, RunRecord, SpecRevisionRecord, ExperimentRecord
from p2r_orchestration.execution_repository import ExecutionRepository
from p2r_orchestration.evidence_registry import EVIDENCE_EVENT_TYPE, register_run_output

CODE_BOUND_EVENT_TYPE = "run.code_bound"
from p2r_orchestration.repair_repository import create_attempt
from p2r_runner import (
    REAL_RECIPE_ID,
    REAL_RECIPE_VERSION,
    SANDBOX_RECIPE_ID,
    SANDBOX_RECIPE_VERSION,
    FixtureRunnerError,
    is_complete_sandbox_code_manifest,
    run_fixture,
    run_real_cpu_baseline,
    run_sandbox_python,
)
from p2r_evaluation import EVALUATOR_VERSION, evaluate_predictions


def _event(db, run_id, typ, payload):
    latest = db.scalar(select(RunEventRecord.seq).where(RunEventRecord.run_id == run_id).order_by(RunEventRecord.seq.desc()).limit(1))
    seq = (latest if latest is not None else -1) + 1
    db.add(RunEventRecord(run_id=run_id, seq=seq, type=typ, payload_json=payload))


def _cancel_running_run(db, run, execution, worker_id, token):
    """Return None when no cancellation is requested, False on fence failure."""
    repository = ExecutionRepository(db)
    if not repository.cancellation_requested(execution):
        return None
    if not repository.cancel(execution.id, runner_id=worker_id, fencing_token=token):
        db.rollback()
        return False
    run.status = "cancelled"
    _event(db, run.id, "run.cancelled", {"status": "cancelled"})
    return True


def process_claimed_run_fixture(session_factory, *, job_id, payload_ref, worker_id="fixture-worker", token, heartbeat=None, artifact_store=None) -> bool:
    return _process_claimed_job(
        session_factory, job_id=job_id, payload_ref=payload_ref, kind="run_fixture",
        worker_id=worker_id, token=token, heartbeat=heartbeat, artifact_store=artifact_store,
    )


def process_next_run_job(session_factory, artifact_store=None, worker_id="fixture-worker", heartbeat=None) -> bool:
    if artifact_store is not None:
        return process_export_job(session_factory, artifact_store, worker_id=worker_id)
    with session_factory() as db:
        job = JobRepository(db).claim(worker_id=worker_id, kinds=("run_fixture", "repair_attempt"))
        if not job:
            db.commit(); return False
        job_id, payload_ref, kind, token = job.id, job.payload_ref, job.kind, job.fencing_token
        db.commit()
    return _process_claimed_job(
        session_factory, job_id=job_id, payload_ref=payload_ref, kind=kind,
        worker_id=worker_id, token=token, heartbeat=heartbeat,
    )


def _process_claimed_job(session_factory, *, job_id, payload_ref, kind, worker_id, token, heartbeat=None, artifact_store=None):
    try:
        return _run_claimed_job(
            session_factory, job_id=job_id, payload_ref=payload_ref, kind=kind,
            worker_id=worker_id, token=token, heartbeat=heartbeat, artifact_store=artifact_store,
        )
    except Exception:
        # Dispatch/handler failures happen outside the claim transaction. Requeue
        # only the exact owner/fence that made this attempt; never touch a job
        # that has already been taken over by another worker.
        with session_factory.begin() as db:
            JobRepository(db).requeue_owned(
                job_id, worker_id=worker_id, fencing_token=token,
            )
        return False


class _LostFence(RuntimeError):
    pass


def _guard_fixture(db, job_id, worker_id, token):
    # A fenced UPDATE serializes each short phase with ownership changes.
    if not JobRepository(db).heartbeat(job_id, worker_id, token):
        raise _LostFence()


def _fail_fixture_after_external_error(session_factory, *, job_id, run_id, worker_id, token):
    with session_factory.begin() as db:
        _guard_fixture(db, job_id, worker_id, token)
        run = db.get(RunRecord, run_id)
        execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
        if run is None or execution is None:
            raise _LostFence()
        cancellation = _cancel_running_run(db, run, execution, worker_id, token)
        if cancellation is False:
            raise _LostFence()
        if cancellation:
            return True
        if run.status not in {"running", "evaluating"}:
            raise _LostFence()
        error = {"code": "PIPELINE_FAILED", "message": "The controlled pipeline failed."}
        if not ExecutionRepository(db).finish(execution.id, runner_id=worker_id, fencing_token=token, status="failed", error_json=error):
            raise _LostFence()
        if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="PIPELINE_FAILED"):
            raise _LostFence()
        run.status = "failed"
        run.error_json = error
        _event(db, run.id, "run.terminal", {"status": "failed"})
        return True


def _execution_handle(recipe_id: str | None, job_id: str, token: int) -> str:
    """Name the execution after what actually runs, so the audit trail never
    shows a fixture handle for a sandbox or real-recipe execution."""
    prefix = ("sandbox" if recipe_id == SANDBOX_RECIPE_ID
              else "local-cpu" if recipe_id == REAL_RECIPE_ID
              else "fixture")
    return f"{prefix}:{job_id}:{token}"


def _run_claimed_job(session_factory, *, job_id, payload_ref, kind, worker_id, token, heartbeat=None, artifact_store=None):
    if kind == "run_fixture":
        # Publish the attempt/execution and running state in a short transaction.
        with session_factory.begin() as db:
            _guard_fixture(db, job_id, worker_id, token)
            run = db.get(RunRecord, payload_ref)
            if not run:
                JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="run not found")
                return False
            if run.status in {"completed", "failed", "cancelled", "awaiting_revision"}:
                if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                    JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token)
                    return False
                return True
            if run.status == "cancel_requested":
                execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
                if execution is not None and execution.status == "running":
                    # The job guard owns the new lease; retire the previous
                    # fixture execution using its own exact execution fence.
                    if not ExecutionRepository(db).finish(
                        execution.id, runner_id=execution.runner_id,
                        fencing_token=execution.fencing_token, status="cancelled",
                        error_json={"code": "EXECUTION_CANCELLED"},
                    ):
                        raise _LostFence()
                run.status = "cancelled"
                _event(db, run.id, "run.cancelled", {"status": "cancelled"})
                if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                    raise _LostFence()
                return True
            revision = db.get(SpecRevisionRecord, run.revision_id)
            experiment = db.get(ExperimentRecord, run.experiment_id)
            recipe_id = experiment.recipe_id if experiment else None
            recipe_version = experiment.recipe_version if experiment else None
            # Real-recipe parameters come only from the confirmed spec's
            # training block: just the four schema keys, non-None values only,
            # passed through unconverted so the runner's own schema validation
            # — not a silent coercion here — rejects anything malformed.
            real_parameters = {}
            if recipe_id == REAL_RECIPE_ID and revision is not None:
                training = revision.spec_json.get("training", {}) if isinstance(revision.spec_json, dict) else {}
                if isinstance(training, dict):
                    for key in ("seed", "max_epochs", "learning_rate", "batch_size"):
                        value = training.get(key)
                        if value is not None:
                            real_parameters[key] = value
            # Sandbox-recipe parameters follow the same confirmed-spec-only
            # rule: just the two schema keys, non-None values, unconverted.
            sandbox_parameters = {}
            if recipe_id == SANDBOX_RECIPE_ID and revision is not None:
                training = revision.spec_json.get("training", {}) if isinstance(revision.spec_json, dict) else {}
                if isinstance(training, dict):
                    for key in ("seed", "max_seconds"):
                        value = training.get(key)
                        if value is not None:
                            sandbox_parameters[key] = value
            # The sandbox recipe executes this experiment's model-generated
            # code: remember the latest model attempt's code manifest here,
            # inside the transaction; the bytes are fetched (and verified)
            # only after the fence commits.
            sandbox_manifest = None
            sandbox_attempt_id = None
            if recipe_id == SANDBOX_RECIPE_ID and experiment is not None:
                model_attempts = db.scalars(
                    select(AttemptRecord).where(
                        AttemptRecord.experiment_id == experiment.id,
                        AttemptRecord.source == "model",
                        AttemptRecord.run_id.is_(None),
                    ).order_by(
                        AttemptRecord.attempt_index.desc(),
                        AttemptRecord.created_at.desc(),
                        AttemptRecord.id.desc(),
                    )
                ).all()
                for model_attempt in model_attempts:
                    if isinstance(model_attempt.code_manifest_json, dict) and model_attempt.code_manifest_json:
                        sandbox_manifest = model_attempt.code_manifest_json
                        sandbox_attempt_id = model_attempt.id
                        break
            attempt = db.scalar(select(AttemptRecord).where(AttemptRecord.run_id == run.id, AttemptRecord.attempt_index == 0))
            if attempt is None:
                attempt = create_attempt(db, run_id=run.id, attempt_index=0, source="template", status="candidate", base_code_hash=run.manifest_hash)
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
            if execution is None:
                execution = ExecutionRecord(
                    run_id=run.id, attempt_id=attempt.id, job_id=job_id, status="running",
                    runner_id=worker_id, fencing_token=token, handle=_execution_handle(recipe_id, job_id, token),
                    started_at=datetime.now(timezone.utc), lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
                )
                db.add(execution)
            else:
                execution.status = "running"
                execution.runner_id = worker_id
                execution.fencing_token = token
                execution.started_at = execution.started_at or datetime.now(timezone.utc)
                execution.lease_until = datetime.now(timezone.utc) + timedelta(seconds=60)
                execution.finished_at = None
                execution.error_json = None
                execution.handle = _execution_handle(recipe_id, job_id, token)
            run.status = "running"
            _event(db, run.id, "stage.changed", {"status": "running"})

        try:
            if recipe_id == SANDBOX_RECIPE_ID and recipe_version == SANDBOX_RECIPE_VERSION:
                # Model code is untrusted input: run exactly the content-
                # addressed bytes the attempt manifest names, after verifying
                # the manifest and the stored bytes agree. Any gap (no store,
                # no complete manifest, missing/changed artifact) fails the
                # run visibly through the shared external-error path below.
                if artifact_store is None:
                    raise FixtureRunnerError("artifact store unavailable for the sandbox recipe")
                if not is_complete_sandbox_code_manifest(sandbox_manifest):
                    raise FixtureRunnerError("no model code attempt with a complete code manifest")
                code_bytes = artifact_store.open_bytes(sandbox_manifest["code_artifact_id"])
                code_sha256 = hashlib.sha256(code_bytes).hexdigest()
                if code_sha256 != sandbox_manifest["code_sha256"] or code_sha256 != sandbox_manifest["code_artifact_id"]:
                    raise FixtureRunnerError("model code bytes do not match the attempt manifest")
                result = run_sandbox_python(
                    recipe_id=recipe_id, recipe_version=recipe_version,
                    parameters=sandbox_parameters,
                    code=code_bytes.decode("utf-8"),
                    artifact_store=artifact_store,
                    attempt_manifest=sandbox_manifest,
                )
            elif recipe_id == REAL_RECIPE_ID and recipe_version == REAL_RECIPE_VERSION:
                result = run_real_cpu_baseline(
                    recipe_id=recipe_id, recipe_version=recipe_version,
                    parameters=real_parameters,
                )
            else:
                result = run_fixture(recipe_id="offline_fixture", recipe_version="1")
        except Exception:
            return _fail_fixture_after_external_error(session_factory, job_id=job_id, run_id=payload_ref, worker_id=worker_id, token=token)

        # Re-open after the fixture so an HTTP cancel in another session is visible.
        with session_factory.begin() as db:
            _guard_fixture(db, job_id, worker_id, token)
            run = db.get(RunRecord, payload_ref)
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
            if execution is None or run is None:
                return False
            if run.status == "cancel_requested":
                if not ExecutionRepository(db).cancel(execution.id, runner_id=worker_id, fencing_token=token):
                    return False
                run.status = "cancelled"
                _event(db, run.id, "run.cancelled", {"status": "cancelled"})
                return True
            if run.status != "running" or execution.runner_id != worker_id or execution.fencing_token != token:
                return False
            run.status = "evaluating"
            _event(db, run.id, "stage.changed", {"status": "evaluating"})

        try:
            # Real recipes carry their own ground-truth labels; the fixture has
            # none and keeps being scored against the fixture label set.
            labels = result.get("labels")
            evaluated = (evaluate_predictions(result["predictions"], labels=labels)
                         if labels is not None else evaluate_predictions(result["predictions"]))
        except Exception:
            return _fail_fixture_after_external_error(session_factory, job_id=job_id, run_id=payload_ref, worker_id=worker_id, token=token)

        # Content-address the runner's real output as this Run's evidence before
        # the terminal transaction. A store failure fails the run like any other
        # external error; a cancelled run leaves only a harmless orphan object.
        evidence_entries = None
        if artifact_store is not None:
            try:
                evidence_entries = register_run_output(payload_ref, result, artifact_store)
            except Exception:
                return _fail_fixture_after_external_error(session_factory, job_id=job_id, run_id=payload_ref, worker_id=worker_id, token=token)

        with session_factory.begin() as db:
            _guard_fixture(db, job_id, worker_id, token)
            run = db.get(RunRecord, payload_ref)
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
            attempt = db.scalar(select(AttemptRecord).where(AttemptRecord.run_id == payload_ref, AttemptRecord.attempt_index == 0))
            if execution is None or run is None or attempt is None:
                return False
            if run.status == "cancel_requested":
                if not ExecutionRepository(db).cancel(execution.id, runner_id=worker_id, fencing_token=token):
                    return False
                run.status = "cancelled"
                _event(db, run.id, "run.cancelled", {"status": "cancelled"})
                return True
            if run.status != "evaluating" or execution.runner_id != worker_id or execution.fencing_token != token:
                return False
            db.add(EvaluationRecord(run_id=run.id, metrics_json=evaluated["metrics"], verdict=evaluated["verdict"], metrics_hash=evaluated["metrics_hash"], evaluator_version=result.get("evaluator_version") or EVALUATOR_VERSION))
            run.metrics_hash = evaluated["metrics_hash"]
            run.verdict = evaluated["verdict"]
            run.status = "completed"
            attempt.status = "validated"
            execution.status = "succeeded"
            execution.finished_at = datetime.now(timezone.utc)
            execution.lease_until = None
            if evidence_entries:
                _event(db, run.id, EVIDENCE_EVENT_TYPE, {"evidence": evidence_entries})
            if sandbox_attempt_id is not None and isinstance(sandbox_manifest, dict):
                # Bind this Run to the exact model attempt whose content-
                # addressed bytes were executed, so the audit trail names the
                # code instead of leaving only a template slot attempt.
                _event(db, run.id, CODE_BOUND_EVENT_TYPE, {
                    "attempt_id": sandbox_attempt_id,
                    "code_sha256": sandbox_manifest.get("code_sha256"),
                    "generator_model": sandbox_manifest.get("generator_model"),
                })
            _event(db, run.id, "run.terminal", {"status": "completed"})
            if heartbeat is not None and not heartbeat(job_id=job_id, worker_id=worker_id, fencing_token=token):
                raise _LostFence()
            if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                raise _LostFence()
            return True

    with session_factory() as db:
        if kind == "repair_attempt":
            proposal = db.get(RepairProposalRecord, payload_ref)
            if proposal is None or proposal.status != "accepted":
                ok = JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="accepted repair proposal not found")
                if not ok:
                    db.rollback(); JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token); db.commit(); return False
                db.commit(); return True
            run = db.get(RunRecord, proposal.run_id)
            if run is None:
                ok = JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="run not found")
                if not ok:
                    db.rollback(); JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token); db.commit(); return False
                db.commit(); return False
            existing = db.scalar(select(AttemptRecord).where(
                AttemptRecord.run_id == run.id,
                AttemptRecord.repair_ordinal == proposal.repair_ordinal,
            ))
            if existing is None:
                latest = db.scalar(select(AttemptRecord.attempt_index).where(AttemptRecord.run_id == run.id).order_by(AttemptRecord.attempt_index.desc()).limit(1))
                existing = create_attempt(
                    db,
                    run_id=run.id,
                    attempt_index=(latest if latest is not None else -1) + 1,
                    source="repair",
                    repair_ordinal=proposal.repair_ordinal,
                    status="validated",
                    base_code_hash=proposal.base_code_hash,
                    patch_hash=proposal.patch_hash,
                    validation_json={"ok": True, "fixture": True},
                    code_manifest_json={"patch_hash": proposal.patch_hash},
                )
            if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                db.rollback(); JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token); db.commit(); return False
            db.commit(); return True
        if kind == "export_report_json":
            export = db.get(ExportRecord, payload_ref)
            if not export:
                if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="export not found"):
                    db.rollback()
                    return False
                db.commit()
                return False
            try:
                report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == export.run_id))
                if not report:
                    raise RuntimeError("report not ready")
                data = json.dumps(report.content_json, ensure_ascii=False, sort_keys=True)
                if isinstance(data, str):
                    data = data.encode()
                if artifact_store is None:
                    raise RuntimeError("artifact store unavailable")
                address = artifact_store.put_bytes(data)
                export.artifact_id = address.sha256
                export.storage_key = str(address.path)
                export.sha256 = address.sha256
                export.bytes = address.bytes
                export.media_type = "application/json"
                item = {"format": "report_json", "status": "ready", "artifact_id": address.sha256, "bytes": address.bytes}
                export.items_json = [item]
                export.status = "succeeded"
            except Exception as exc:
                export.status = "failed"
                export.error_json = {
                    "code": "EXPORT_FAILED",
                    "message": "The export failed.",
                }
                if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="EXPORT_FAILED"):
                    db.rollback()
                    return False
                db.commit()
                return True
            if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                db.rollback()
                return False
            db.commit()
            return True
        if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="unsupported job kind"):
            db.rollback()
            return False
        db.commit()
        return False
