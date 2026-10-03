from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import Job


def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    init_db(engine)
    return create_session_factory(engine)


def test_claim_increments_fencing_token_and_old_token_cannot_complete_or_fail(tmp_path):
    factory = db(tmp_path)
    with factory.begin() as session:
        job = JobRepository(session).enqueue(kind="fixture", payload_ref="run-1")
        first = JobRepository(session).claim(worker_id="worker-a", lease_seconds=1)
        first_token = first.fencing_token
        first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    with factory.begin() as session:
        assert JobRepository(session).requeue_expired() == 1
        second = JobRepository(session).claim(worker_id="worker-b")
        assert second.fencing_token == first_token + 1
        assert not JobRepository(session).complete(job.id, worker_id="worker-b", fencing_token=first_token)
        assert not JobRepository(session).fail(job.id, worker_id="worker-b", fencing_token=first_token, error="stale")
        assert JobRepository(session).complete(job.id, worker_id="worker-b", fencing_token=second.fencing_token)

    with factory() as session:
        assert session.scalar(select(Job).where(Job.id == job.id)).status == "succeeded"
