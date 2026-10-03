from __future__ import annotations

from datetime import timedelta

from p2r_orchestration.execution_repository import ExecutionRepository
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import AttemptRecord, ExecutionRecord, Job, utcnow

from tests.integration.phase02.test_run_worker import factory, make_run


def _execution(sessions):
    with sessions.begin() as db:
        run = make_run(db)
        attempt = AttemptRecord(
            run_id=run.id,
            attempt_index=0,
            source="template",
            status="candidate",
            base_code_hash=run.manifest_hash,
            validation_json={},
            code_manifest_json={},
        )
        db.add(attempt)
        db.flush()
        job = JobRepository(db).enqueue(kind="execution", payload_ref="pending")
        execution = ExecutionRecord(run_id=run.id, attempt_id=attempt.id, job_id=job.id, fencing_token=0)
        db.add(execution)
        db.flush()
        job.payload_ref = execution.id
        return job.id, execution.id


def test_execution_lifecycle_rejects_stale_runner_and_accepts_current_fence(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    with sessions.begin() as db:
        job = db.get(Job, job_id)
        job.status = "running"
        job.leased_by = "runner"
        job.fencing_token = 4
        execution = db.get(ExecutionRecord, execution_id)
        execution.fencing_token = 4
        repo = ExecutionRepository(db)
        assert repo.start(execution_id, runner_id="runner", fencing_token=4)
        assert not repo.heartbeat(execution_id, runner_id="other", fencing_token=4)
        assert repo.heartbeat(execution_id, runner_id="runner", fencing_token=4)
        assert not repo.finish(execution_id, runner_id="runner", fencing_token=3, status="succeeded")
        assert repo.finish(execution_id, runner_id="runner", fencing_token=4, status="succeeded")


def test_cancel_rejects_stale_job_without_mutating_execution(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    with sessions.begin() as db:
        job = db.get(Job, job_id)
        job.status = "running"
        job.leased_by = "runner"
        job.fencing_token = 7
        execution = db.get(ExecutionRecord, execution_id)
        execution.status = "running"
        execution.runner_id = "runner"
        execution.fencing_token = 7
        job.leased_by = "other-runner"

        assert not ExecutionRepository(db).cancel(execution_id, runner_id="runner", fencing_token=7)
        assert execution.status == "running"
        assert job.status == "running"
        assert job.leased_by == "other-runner"

    with sessions() as db:
        assert db.get(ExecutionRecord, execution_id).status == "running"
        assert db.get(Job, job_id).status == "running"


def test_recover_expired_execution_requeues_same_fenced_job_once(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    with sessions.begin() as db:
        job = db.get(Job, job_id)
        job.status = "running"
        job.leased_by = "runner"
        job.fencing_token = 7
        execution = db.get(ExecutionRecord, execution_id)
        execution.fencing_token = 7
        execution.status = "running"
        execution.lease_until = utcnow() - timedelta(seconds=1)
        assert ExecutionRepository(db).recover_expired() == 1
        assert execution.status == "recovered"
        assert db.get(Job, job_id).status == "queued"
        assert ExecutionRepository(db).recover_expired() == 0


def test_recover_expired_execution_preserves_running_state_when_job_fence_rejects(monkeypatch, tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    with sessions.begin() as db:
        job = db.get(Job, job_id)
        job.status = "running"
        job.leased_by = "runner"
        job.fencing_token = 7
        execution = db.get(ExecutionRecord, execution_id)
        execution.fencing_token = 7
        execution.status = "running"
        execution.runner_id = "runner"
        execution.lease_until = utcnow() - timedelta(seconds=1)

        monkeypatch.setattr(JobRepository, "fail", lambda *args, **kwargs: False)
        assert ExecutionRepository(db).recover_expired() == 0
        assert execution.status == "running"
        assert db.get(Job, job_id).status == "running"
        assert db.get(Job, job_id).leased_by == "runner"

    with sessions() as db:
        assert db.get(ExecutionRecord, execution_id).status == "running"
        assert db.get(Job, job_id).status == "running"
        assert db.get(Job, job_id).leased_by == "runner"
