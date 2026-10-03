from __future__ import annotations

from sqlalchemy import select

from p2r_orchestration.execution_repository import ExecutionRepository
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import AttemptRecord, ExecutionRecord, Job
from p2r_runner_service import ControlledRunner, RunnerResult

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
        execution = ExecutionRecord(
            run_id=run.id,
            attempt_id=attempt.id,
            job_id=job.id,
            fencing_token=1,
        )
        db.add(execution)
        db.flush()
        job.payload_ref = execution.id
        return job.id, execution.id


def _states(sessions, job_id, execution_id):
    with sessions() as db:
        return db.get(Job, job_id), db.get(ExecutionRecord, execution_id)


def test_controlled_runner_executes_and_completes_fenced_job(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    seen = []

    def execute(execution):
        seen.append(execution.id)
        return RunnerResult("succeeded")

    assert ControlledRunner(sessions, runner_id="runner-a").run_once(execute)

    job, execution = _states(sessions, job_id, execution_id)
    assert seen == [execution_id]
    assert execution.status == "succeeded"
    assert execution.runner_id == "runner-a"
    assert job.status == "succeeded"
    assert job.leased_by is None


def test_heartbeat_failure_rolls_back_started_execution_and_requeues_job(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    seen = []

    def heartbeat(**kwargs):
        seen.append(kwargs)
        return False

    executed = []
    assert not ControlledRunner(sessions, runner_id="runner-a").run_once(
        lambda execution: executed.append(execution.id) or RunnerResult("succeeded"),
        heartbeat=heartbeat,
    )

    job, execution = _states(sessions, job_id, execution_id)
    assert executed == []
    assert seen == [{
        "execution_id": execution_id,
        "job_id": job_id,
        "runner_id": "runner-a",
        "fencing_token": 1,
    }]
    assert execution.status == "queued"
    assert execution.runner_id is None
    assert job.status == "queued"
    assert job.leased_by is None


def test_cancellation_before_execute_finishes_execution_without_calling_execute(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    executed = []

    def cancellation_probe(execution):
        return True

    assert ControlledRunner(sessions, runner_id="runner-a").run_once(
        lambda execution: executed.append(execution.id) or RunnerResult("succeeded"),
        cancellation_probe=cancellation_probe,
    )

    job, execution = _states(sessions, job_id, execution_id)
    assert executed == []
    assert execution.status == "cancelled"
    assert execution.error_json == {"code": "EXECUTION_CANCELLED"}
    assert job.status == "cancelled"
    assert job.leased_by is None


def test_cancellation_after_execute_discards_result_and_cancels_job(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    probes = []
    executed = []

    def cancellation_probe(execution):
        probes.append(execution.id)
        return len(probes) == 2

    assert ControlledRunner(sessions, runner_id="runner-a").run_once(
        lambda execution: executed.append(execution.id) or RunnerResult("succeeded"),
        cancellation_probe=cancellation_probe,
    )

    job, execution = _states(sessions, job_id, execution_id)
    assert executed == [execution_id]
    assert probes == [execution_id, execution_id]
    assert execution.status == "cancelled"
    assert job.status == "cancelled"


def test_stale_execution_fence_aborts_before_business_execution(tmp_path):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    with sessions.begin() as db:
        db.get(ExecutionRecord, execution_id).fencing_token = 99

    executed = []
    assert not ControlledRunner(sessions, runner_id="runner-a").run_once(
        lambda execution: executed.append(execution.id) or RunnerResult("succeeded")
    )

    job, execution = _states(sessions, job_id, execution_id)
    assert executed == []
    assert execution.status == "queued"
    assert job.status == "queued"
    assert job.leased_by is None


def test_stale_job_owner_rolls_back_execution_before_business_execution(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    job_id, execution_id = _execution(sessions)
    original_claim = JobRepository.claim

    def claim_with_stale_owner(self, *, worker_id, lease_seconds=60):
        job = original_claim(self, worker_id=worker_id, lease_seconds=lease_seconds)
        job.leased_by = "another-runner"
        return job

    monkeypatch.setattr(JobRepository, "claim", claim_with_stale_owner)
    executed = []
    assert not ControlledRunner(sessions, runner_id="runner-a").run_once(
        lambda execution: executed.append(execution.id) or RunnerResult("succeeded")
    )

    job, execution = _states(sessions, job_id, execution_id)
    assert executed == []
    assert execution.status == "queued"
    assert job.status == "queued"
    assert job.leased_by is None
