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


def test_job_heartbeat_extends_lease_with_current_fence(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        job = JobRepository(session).enqueue(kind="heartbeat", payload_ref="a")
        claimed = JobRepository(session).claim(worker_id="worker-a", lease_seconds=1)
        token = claimed.fencing_token
        original_lease_until = claimed.lease_until
        assert JobRepository(session).heartbeat(
            job.id, worker_id="worker-a", fencing_token=token, lease_seconds=60
        )
        assert claimed.lease_until > original_lease_until


def test_stale_fence_cannot_heartbeat_or_complete_reclaimed_job(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        job = JobRepository(session).enqueue(kind="heartbeat", payload_ref="a")
        first = JobRepository(session).claim(worker_id="worker-a", lease_seconds=1)
        old_token = first.fencing_token
        first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert JobRepository(session).requeue_expired() == 1
        second = JobRepository(session).claim(worker_id="worker-b")
        assert second.fencing_token == old_token + 1
        repo = JobRepository(session)
        assert not repo.heartbeat(job.id, worker_id="worker-b", fencing_token=old_token)
        assert not repo.complete(job.id, worker_id="worker-b", fencing_token=old_token)
        assert repo.complete(job.id, worker_id="worker-b", fencing_token=second.fencing_token)


def test_requeue_owned_rejects_stale_fence_and_wrong_owner(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        repo = JobRepository(session)
        job = repo.enqueue(kind="run_fixture", payload_ref="fixture")
        first = repo.claim(worker_id="worker-a")
        job_id, old_token = job.id, first.fencing_token
        first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert repo.requeue_expired() == 1
        second = repo.claim(worker_id="worker-a")
        current_token = second.fencing_token
    with factory.begin() as session:
        repo = JobRepository(session)
        assert not repo.requeue_owned(job_id, worker_id="worker-a", fencing_token=old_token)
        assert not repo.requeue_owned(job_id, worker_id="worker-b", fencing_token=current_token)
    with factory() as session:
        stored = session.get(Job, job_id)
        assert stored.status == "running"
        assert stored.fencing_token == current_token
        assert stored.leased_by == "worker-a"
    with factory.begin() as session:
        assert JobRepository(session).requeue_owned(job_id, worker_id="worker-a", fencing_token=current_token)
    with factory.begin() as session:
        reclaimed = JobRepository(session).claim(worker_id="worker-b")
        assert reclaimed.id == job_id
        assert reclaimed.fencing_token == current_token + 1


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


@pytest.mark.parametrize("handler_fails", [False, True])
def test_worker_stale_execution_cannot_finish_reclaimed_lease(tmp_path, handler_fails):
    factory = db(tmp_path)
    worker = WorkerService(factory)
    with factory.begin() as session:
        job_id = JobRepository(session).enqueue(kind="known", payload_ref="ref-1").id

    def reclaim_during_handler(payload_ref):
        assert payload_ref == "ref-1"
        with factory.begin() as session:
            job = session.get(Job, job_id)
            old_token = job.fencing_token
            job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.flush()
            repo = JobRepository(session)
            assert repo.requeue_expired() == 1
            reclaimed = repo.claim(worker_id="same-worker")
            assert reclaimed.id == job_id
            assert reclaimed.fencing_token == old_token + 1
        if handler_fails:
            raise RuntimeError("controlled handler failure")

    worker.register("known", reclaim_during_handler)
    if handler_fails:
        with pytest.raises(RuntimeError, match="controlled handler failure"):
            worker.run_once(worker_id="same-worker")
    else:
        worker.run_once(worker_id="same-worker")
    with factory() as session:
        stored = session.get(Job, job_id)
        assert stored.status == "running"
        assert stored.leased_by == "same-worker"
        assert stored.fencing_token == 2
        assert stored.lease_until is not None
        assert stored.error is None


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
