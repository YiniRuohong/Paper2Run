from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from p2r_persistence.models import ExecutionRecord, RunEventRecord, RunRecord
from p2r_persistence.job_repository import JobRepository
from p2r_orchestration.execution_repository import ExecutionRepository


_ALLOWED_RESULTS = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class RunnerResult:
    status: str
    error_json: dict | None = None

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_RESULTS:
            raise ValueError("invalid runner result status")


class ControlledRunner:
    """Small cooperative runner adapter with explicit heartbeat and fence checks."""

    def __init__(self, session_factory, runner_id: str = "runner-1", *, heartbeat=None, cancellation_probe=None):
        self.session_factory = session_factory
        self.runner_id = runner_id
        self.heartbeat_callback = heartbeat
        self.cancellation_probe = cancellation_probe

    def run_once(self, execute: Callable[[ExecutionRecord], RunnerResult], *, heartbeat=None, cancellation_probe=None) -> bool:
        heartbeat = self.heartbeat_callback if heartbeat is None else heartbeat
        cancellation_probe = self.cancellation_probe if cancellation_probe is None else cancellation_probe
        with self.session_factory.begin() as db:
            job = JobRepository(db).claim(worker_id=self.runner_id)
            if job is None:
                return False
            if job.kind != "execution":
                if not JobRepository(db).fail(job.id, worker_id=self.runner_id, fencing_token=job.fencing_token, error="unsupported runner job", requeue=True):
                    db.rollback()
                    return False
                return False
            execution = db.get(ExecutionRecord, job.payload_ref)
            if execution is None:
                if not JobRepository(db).fail(job.id, worker_id=self.runner_id, fencing_token=job.fencing_token, error="execution not found"):
                    db.rollback()
                    return False
                return True
            execution_repo = ExecutionRepository(db)
            run = db.get(RunRecord, execution.run_id)
            token = job.fencing_token
            if not execution_repo.start(execution.id, runner_id=self.runner_id, fencing_token=token):
                db.rollback()
                return False
            db.flush()

            if (cancellation_probe is not None and cancellation_probe(execution)) or execution_repo.cancellation_requested(execution):
                if not execution_repo.cancel(execution.id, runner_id=self.runner_id, fencing_token=token):
                    db.rollback()
                    return False
                if run is not None:
                    run.status = "cancelled"
                return True

            if not self._heartbeat(db, execution_repo, job.id, execution.id, token, heartbeat):
                db.rollback()
                return False

            try:
                result = execute(execution)
            except Exception as exc:
                result = RunnerResult("failed", {"code": "RUNNER_FAILED", "message": str(exc)})

            if (cancellation_probe is not None and cancellation_probe(execution)) or execution_repo.cancellation_requested(execution):
                if not execution_repo.cancel(execution.id, runner_id=self.runner_id, fencing_token=token):
                    db.rollback()
                    return False
                if run is not None:
                    run.status = "cancelled"
                return True

            if not self._heartbeat(db, execution_repo, job.id, execution.id, token, heartbeat):
                db.rollback()
                return False
            if execution_repo.cancellation_requested(execution):
                if not execution_repo.cancel(execution.id, runner_id=self.runner_id, fencing_token=token):
                    db.rollback()
                    return False
                if run is not None:
                    run.status = "cancelled"
                return True
            if not execution_repo.finish(execution.id, runner_id=self.runner_id, fencing_token=token, status=result.status, error_json=result.error_json):
                db.rollback()
                return False
            if not JobRepository(db).complete(job.id, worker_id=self.runner_id, fencing_token=token):
                db.rollback()
                return False
            return True

    def _heartbeat(self, db, execution_repo, job_id, execution_id, fencing_token, callback) -> bool:
        if not execution_repo.heartbeat(execution_id, runner_id=self.runner_id, fencing_token=fencing_token):
            return False
        if not JobRepository(db).heartbeat(job_id, self.runner_id, fencing_token):
            return False
        if callback is None:
            return True
        return bool(callback(
            execution_id=execution_id,
            job_id=job_id,
            runner_id=self.runner_id,
            fencing_token=fencing_token,
        ))
