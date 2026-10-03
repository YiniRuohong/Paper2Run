from __future__ import annotations

from datetime import timedelta
import uuid
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from p2r_persistence.models import ExecutionRecord, Job, RunRecord, utcnow
from p2r_persistence.job_repository import JobRepository


class ExecutionRepository:
    """Fenced execution lease operations; business completion remains transactional."""

    def __init__(self, session: Session):
        self.session = session

    def create_for_job(self, job: Job, *, run_id: str, attempt_id: str) -> ExecutionRecord:
        existing = self.session.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job.id))
        if existing is not None:
            return existing
        row = ExecutionRecord(run_id=run_id, attempt_id=attempt_id, job_id=job.id, status="queued", fencing_token=job.fencing_token)
        self.session.add(row)
        self.session.flush()
        return row

    def start(self, execution_id: str, *, runner_id: str, fencing_token: int, lease_seconds: int = 60, handle: str | None = None) -> bool:
        now = utcnow()
        result = self.session.execute(update(ExecutionRecord).where(
            ExecutionRecord.id == execution_id,
            ExecutionRecord.status == "queued",
            ExecutionRecord.fencing_token == fencing_token,
        ).values(status="running", runner_id=runner_id, lease_until=now + timedelta(seconds=lease_seconds), started_at=now, handle=handle or f"exec:{execution_id}:{uuid.uuid4()}"))
        return result.rowcount == 1

    def heartbeat(self, execution_id: str, *, runner_id: str, fencing_token: int, lease_seconds: int = 60) -> bool:
        result = self.session.execute(update(ExecutionRecord).where(
            ExecutionRecord.id == execution_id,
            ExecutionRecord.status == "running",
            ExecutionRecord.runner_id == runner_id,
            ExecutionRecord.fencing_token == fencing_token,
        ).values(lease_until=utcnow() + timedelta(seconds=lease_seconds)))
        return result.rowcount == 1

    def cancellation_requested(self, execution: ExecutionRecord) -> bool:
        status = self.session.scalar(select(RunRecord.status).where(RunRecord.id == execution.run_id))
        return status == "cancel_requested"

    def finish(self, execution_id: str, *, runner_id: str, fencing_token: int, status: str, error_json: dict | None = None) -> bool:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("invalid execution terminal status")
        result = self.session.execute(update(ExecutionRecord).where(
            ExecutionRecord.id == execution_id,
            ExecutionRecord.status == "running",
            ExecutionRecord.runner_id == runner_id,
            ExecutionRecord.fencing_token == fencing_token,
        ).values(status=status, finished_at=utcnow(), lease_until=None, error_json=error_json))
        return result.rowcount == 1

    def cancel(self, execution_id: str, *, runner_id: str, fencing_token: int) -> bool:
        """Cooperatively cancel a running execution and its fenced job."""
        execution = self.session.get(ExecutionRecord, execution_id)
        if execution is None:
            return False
        job = self.session.get(Job, execution.job_id) if execution.job_id else None
        if (
            execution.status != "running"
            or execution.runner_id != runner_id
            or execution.fencing_token != fencing_token
            or job is None
            or job.fencing_token != fencing_token
            or job.status != "running"
            or job.leased_by != runner_id
        ):
            return False

        now = utcnow()
        result = self.session.execute(update(ExecutionRecord).where(
            ExecutionRecord.id == execution_id,
            ExecutionRecord.status == "running",
            ExecutionRecord.runner_id == runner_id,
            ExecutionRecord.fencing_token == fencing_token,
        ).values(status="cancelled", finished_at=now, lease_until=None, error_json={"code": "EXECUTION_CANCELLED"}))
        if result.rowcount != 1:
            return False
        job.status = "cancelled"
        job.lease_until = None
        job.leased_by = None
        return True

    def recover_expired(self) -> int:
        now = utcnow()
        rows = self.session.scalars(select(ExecutionRecord).where(ExecutionRecord.status == "running", ExecutionRecord.lease_until.is_not(None), ExecutionRecord.lease_until < now)).all()
        recovered = 0
        for row in rows:
            job = self.session.get(Job, row.job_id)
            if job is None or job.status != "running" or job.fencing_token != row.fencing_token:
                continue
            if not JobRepository(self.session).fail(
                job.id,
                worker_id=job.leased_by or "reaper",
                fencing_token=row.fencing_token,
                error="execution lease expired",
                requeue=True,
            ):
                continue
            row.status = "recovered"
            row.finished_at = now
            row.error_json = {"code": "EXECUTION_LEASE_EXPIRED"}
            recovered += 1
        return recovered
