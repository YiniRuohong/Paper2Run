from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select, update

from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import OperationRecord, ReportBuildRecord, ReportRecord, RunRecord
from .report_builder import build_report, report_hash


_input_hash = report_hash
_ERROR = "REPORT_BUILD_FAILED"


def _finish(session_factory, job_id, build_id, run_id, worker_id, token, report=None):
    """Fence and publish in one transaction; any rejected write rolls it all back."""
    with session_factory() as db:
        repository = JobRepository(db)
        if report is None:
            owned = repository.fail(job_id, worker_id=worker_id, fencing_token=token, error=_ERROR)
        else:
            owned = repository.complete(job_id, worker_id=worker_id, fencing_token=token)
        if not owned:
            db.rollback()
            return False
        # Lock/CAS the active slot after acquiring the job fence. No stale worker
        # can publish a failure either, even when ownership changes mid-build.
        active = db.execute(update(RunRecord).where(
            RunRecord.id == run_id, RunRecord.active_report_build_id == build_id,
        ).values(active_report_build_id=build_id))
        if active.rowcount != 1:
            db.rollback()
            return False
        build = db.get(ReportBuildRecord, build_id)
        if build is None or build.run_id != run_id or build.status != "generating":
            db.rollback()
            return False
        operation = db.get(OperationRecord, build.operation_id)
        if operation is None or operation.kind != "report_build" or operation.resource_id != run_id:
            db.rollback()
            return False
        if report is None:
            build.status = "failed"
            build.error_json = {"code": _ERROR}
            operation.status = "failed"
            operation.error_json = {"code": _ERROR}
        else:
            content_hash = report_hash(report)
            existing = db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
            if existing is not None:
                if existing.content_hash != content_hash or report_hash(existing.content_json) != content_hash:
                    raise ValueError(_ERROR)
            else:
                db.add(ReportRecord(run_id=run_id, content_json=report, content_hash=content_hash))
            db.execute(update(RunRecord).where(RunRecord.id == run_id).values(report_hash=content_hash))
            build.status = "ready"
            build.error_json = None
            operation.status = "succeeded"
            operation.error_json = None
        db.commit()
        return True


def process_claimed_report_job(session_factory, *, build_id, job_id, worker_id, token) -> bool:
    return _process_claimed_report_job(
        session_factory, build_id=build_id, job_id=job_id, worker_id=worker_id, token=token,
    )


def process_next_report_job(session_factory, worker_id="report-worker") -> bool:
    with session_factory() as db:
        # SQLite ignores SELECT FOR UPDATE; begin a real write transaction before
        # claim, keeping concurrent report consumers from sharing one generation.
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        job = JobRepository(db).claim(worker_id=worker_id, kinds=("report_build",))
        if job is None:
            db.commit()
            return False
        job_id, token = job.id, job.fencing_token
        build_id = job.payload_ref
        db.commit()
    return _process_claimed_report_job(
        session_factory, build_id=build_id, job_id=job_id,
        worker_id=worker_id, token=token,
    )


def _process_claimed_report_job(session_factory, *, build_id, job_id, worker_id, token) -> bool:
    with session_factory() as db:
        # Acquire the job fence before publishing even an intermediate state.
        # The UPDATE holds ownership stable until this short transaction commits.
        if not JobRepository(db).heartbeat(job_id, worker_id, token):
            db.rollback()
            return False
        build = db.get(ReportBuildRecord, build_id)
        run = db.get(RunRecord, build.run_id) if build else None
        operation = db.get(OperationRecord, build.operation_id) if build else None
        if (build is None or run is None or run.active_report_build_id != build.id
                or operation is None or operation.kind != "report_build"
                or operation.resource_id != run.id):
            JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error=_ERROR)
            db.commit()
            return False
        if build.status in {"ready", "failed"}:
            # Re-delivery never moves terminal business records backwards.
            if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
                db.rollback()
                return False
            db.commit()
            return True
        build_id, run_id = build.id, run.id
        snapshot, input_hash = deepcopy(build.input_json), build.input_hash
        build.status = "generating"
        operation.status = "running"
        db.commit()
    # No database transaction/session is retained while building report bytes.
    try:
        if not isinstance(snapshot, dict) or _input_hash(snapshot) != input_hash or snapshot.get("run_id") != run_id:
            raise ValueError(_ERROR)
        report = build_report(snapshot)
        return _finish(session_factory, job_id, build_id, run_id, worker_id, token, report)
    except Exception:
        return _finish(session_factory, job_id, build_id, run_id, worker_id, token)
