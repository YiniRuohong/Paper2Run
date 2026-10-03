from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Job


class JobRepository:
    """Short-transaction durable job queue operations."""

    def __init__(self, session: Session):
        self.session = session

    def enqueue(
        self,
        *,
        kind: str,
        payload_ref: str,
        idempotency_key: str | None = None,
        available_at: datetime | None = None,
    ) -> Job:
        if idempotency_key:
            existing = self.session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing:
                return existing
        job = Job(
            kind=kind,
            payload_ref=payload_ref,
            idempotency_key=idempotency_key,
            available_at=available_at or datetime.now(timezone.utc),
        )
        try:
            with self.session.begin_nested():
                self.session.add(job)
                self.session.flush()
        except IntegrityError:
            if not idempotency_key:
                raise
            existing = self.session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing is None:
                raise
            return existing
        return job

    def claim(self, *, worker_id: str, lease_seconds: int = 60, kinds: tuple[str, ...] | None = None) -> Job | None:
        now = datetime.now(timezone.utc)
        candidate = self.session.scalar(
            select(Job)
            .where(
                Job.status == "queued",
                Job.available_at <= now,
                *([Job.kind.in_(kinds)] if kinds is not None else []),
                or_(Job.lease_until.is_(None), Job.lease_until <= now),
            )
            .order_by(Job.available_at, Job.created_at)
            .with_for_update()
        )
        if candidate is None:
            return None
        candidate.status = "running"
        candidate.attempt += 1
        candidate.fencing_token += 1
        candidate.leased_by = worker_id
        candidate.lease_until = now + timedelta(seconds=lease_seconds)
        return candidate

    def lease(self, job_id: str, *, worker_id: str, lease_seconds: int = 60, fencing_token: int | None = None) -> bool:
        now = datetime.now(timezone.utc)
        conditions = [Job.id == job_id, Job.status == "running", Job.leased_by == worker_id]
        if fencing_token is not None:
            conditions.append(Job.fencing_token == fencing_token)
        result = self.session.execute(
            update(Job).where(*conditions).values(lease_until=now + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int = 60,
    ) -> bool:
        """Extend a running job lease only for its current owner and fence."""
        now = datetime.now(timezone.utc)
        result = self.session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.leased_by == worker_id,
                Job.fencing_token == fencing_token,
            )
            .values(lease_until=now + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1

    def complete(self, job_id: str, *, worker_id: str, fencing_token: int | None = None) -> bool:
        conditions = [Job.id == job_id, Job.status == "running", Job.leased_by == worker_id]
        if fencing_token is not None:
            conditions.append(Job.fencing_token == fencing_token)
        result = self.session.execute(
            update(Job).where(*conditions).values(status="succeeded", lease_until=None, leased_by=None)
        )
        return result.rowcount == 1

    def fail(self, job_id: str, *, worker_id: str, error: str, requeue: bool = False, fencing_token: int | None = None) -> bool:
        values: dict[str, Any] = {
            "status": "queued" if requeue else "failed",
            "lease_until": None,
            "leased_by": None,
            "error": error,
        }
        if requeue:
            values["available_at"] = datetime.now(timezone.utc)
        conditions = [Job.id == job_id, Job.status == "running", Job.leased_by == worker_id]
        if fencing_token is not None:
            conditions.append(Job.fencing_token == fencing_token)
        result = self.session.execute(
            update(Job).where(*conditions).values(**values)
        )
        return result.rowcount == 1

    def requeue_owned(self, job_id: str, *, worker_id: str, fencing_token: int | None = None) -> bool:
        """Return a just-claimed job to the queue after a fenced rollback."""
        conditions = [Job.id == job_id, Job.status == "running", Job.leased_by == worker_id]
        if fencing_token is not None:
            conditions.append(Job.fencing_token == fencing_token)
        result = self.session.execute(
            update(Job).where(*conditions).values(
                status="queued", lease_until=None, leased_by=None,
                available_at=datetime.now(timezone.utc),
            )
        )
        return result.rowcount == 1

    def requeue_expired(self, *, kinds: tuple[str, ...] | None = None) -> int:
        now = datetime.now(timezone.utc)
        conditions = [Job.status == "running", Job.lease_until.is_not(None), Job.lease_until < now]
        if kinds is not None:
            conditions.append(Job.kind.in_(kinds))
        result = self.session.execute(
            update(Job)
            .where(*conditions)
            .values(status="queued", lease_until=None, leased_by=None, available_at=now)
        )
        return result.rowcount
