from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import Job
from p2r_persistence.repositories import JobRepository
from p2r_persistence.worker import UnknownJobKind, WorkerService


def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    init_db(engine)
    return create_session_factory(engine)


def test_job_claim_lease_and_complete(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        job = JobRepository(session).enqueue(
            kind="artifact.build", payload_ref="artifact:123", idempotency_key="op-1"
        )
        assert job.status == "queued"

    with factory.begin() as session:
        claimed = JobRepository(session).claim(lease_seconds=30, worker_id="worker-a")
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "running"
        assert claimed.attempt == 1
        assert claimed.lease_until is not None
        assert claimed.leased_by == "worker-a"

    with factory.begin() as session:
        assert JobRepository(session).lease(job.id, lease_seconds=60, worker_id="worker-a")
        JobRepository(session).complete(job.id, worker_id="worker-a")

    with factory() as session:
        stored = session.scalar(select(Job).where(Job.id == job.id))
        assert stored.status == "succeeded"
        assert stored.lease_until is None


def test_job_claim_respects_availability_and_idempotency(tmp_path):
    factory = db(tmp_path)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with factory.begin() as session:
        repo = JobRepository(session)
        repo.enqueue(kind="later", payload_ref="a", available_at=future, idempotency_key="same")
        duplicate = repo.enqueue(kind="later", payload_ref="b", idempotency_key="same")
        assert duplicate.payload_ref == "a"
        assert repo.claim(worker_id="worker") is None


def test_expired_running_job_is_requeued_and_claimable(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        job = JobRepository(session).enqueue(kind="retry", payload_ref="a")
        claimed = JobRepository(session).claim(worker_id="worker", lease_seconds=1)
        claimed.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    with factory.begin() as session:
        assert JobRepository(session).requeue_expired() == 1
        claimed = JobRepository(session).claim(worker_id="worker-2")
        assert claimed.id == job.id
        assert claimed.attempt == 2


def test_worker_dispatch_uses_registered_handler_only(tmp_path):
    factory = db(tmp_path)
    seen: list[str] = []
    worker = WorkerService(factory)
    worker.register("known", lambda payload_ref: seen.append(payload_ref))

    with factory.begin() as session:
        job = JobRepository(session).enqueue(kind="known", payload_ref="ref-1")
    worker.run_once()
    assert seen == ["ref-1"]

    with factory.begin() as session:
        unknown = JobRepository(session).enqueue(kind="not-registered", payload_ref="ref-2")
    with pytest.raises(UnknownJobKind):
        worker.run_once()
    with factory() as session:
        assert session.get(Job, unknown.id).status == "failed"
