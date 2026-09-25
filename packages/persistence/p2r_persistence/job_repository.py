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

    def claim(self, *, worker_id: str, lease_seconds: int = 60) -> Job | None:
        now = datetime.now(timezone.utc)
        candidate = self.session.scalar(
            select(Job)
            .where(
                Job.status == "queued",
                Job.available_at <= now,
                or_(Job.lease_until.is_(None), Job.lease_until <= now),
            )
            .order_by(Job.available_at, Job.created_at)
            .with_for_update()
        )
        if candidate is None:
            return None
        candidate.status = "running"
        candidate.attempt += 1
        candidate.leased_by = worker_id
        candidate.lease_until = now + timedelta(seconds=lease_seconds)
        return candidate

    def lease(self, job_id: str, *, worker_id: str, lease_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        result = self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.leased_by == worker_id)
            .values(lease_until=now + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1

    def complete(self, job_id: str, *, worker_id: str) -> bool:
        result = self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.leased_by == worker_id)
            .values(status="succeeded", lease_until=None, leased_by=None)
        )
        return result.rowcount == 1

    def fail(self, job_id: str, *, worker_id: str, error: str, requeue: bool = False) -> bool:
        values: dict[str, Any] = {
            "status": "queued" if requeue else "failed",
            "lease_until": None,
            "leased_by": None,
            "error": error,
        }
        if requeue:
            values["available_at"] = datetime.now(timezone.utc)
        result = self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.leased_by == worker_id)
            .values(**values)
        )
        return result.rowcount == 1

    def requeue_expired(self) -> int:
        now = datetime.now(timezone.utc)
        result = self.session.execute(
            update(Job)
            .where(Job.status == "running", Job.lease_until < now)
            .values(status="queued", lease_until=None, leased_by=None, available_at=now)
        )
        return result.rowcount
