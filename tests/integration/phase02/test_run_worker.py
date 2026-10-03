import json

import pytest
from sqlalchemy import select

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    EvaluationRecord,
    ExecutionRecord,
    ExperimentRecord,
    ExtractionRecord,
    Job,
    PaperRecord,
    ProjectRecord,
    ReportRecord,
    RepairProposalRecord,
    RunEventRecord,
    RunRecord,
    AttemptRecord,
    SpecRevisionRecord,
    UserRecord,
)
from p2r_orchestration.run_worker import process_next_run_job


def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite3'}")
    init_db(engine)
    return create_session_factory(engine)


def make_run(session, *, status="queued"):
    user = UserRecord(display_name="worker test", can_create_project=True)
    session.add(user); session.flush()
    project = ProjectRecord(name="worker project", created_by=user.id)
    session.add(project); session.flush()
    paper = PaperRecord(project_id=project.id, sha256="a" * 64, artifact_key="paper", bytes=1, page_count=1)
    session.add(paper); session.flush()
    extraction = ExtractionRecord(paper_id=paper.id, content_sha256="b" * 64, payload_json={})
    session.add(extraction); session.flush()
    experiment = ExperimentRecord(project_id=project.id, paper_id=paper.id, extraction_id=extraction.id, candidate_experiment_id="candidate", recipe_id="offline_fixture", recipe_version="1")
    session.add(experiment); session.flush()
    revision = SpecRevisionRecord(experiment_id=experiment.id, revision_no=1, spec_hash="c" * 64, policy_hash="d" * 64, spec_json={})
    session.add(revision); session.flush()
    run = RunRecord(project_id=project.id, experiment_id=experiment.id, revision_id=revision.id, status=status, manifest_hash="e" * 64)
    session.add(run); session.flush()
    session.add(RunEventRecord(run_id=run.id, seq=0, type="run.created", payload_json={"status": status}))
    return run


@pytest.mark.parametrize("entry", ["next", "claimed"])
@pytest.mark.parametrize("stolen", [False, True])
def test_dispatch_exception_requeues_only_current_claim(tmp_path, monkeypatch, entry, stolen):
    import p2r_orchestration.run_worker as worker

    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id
    token = 1
    if entry == "claimed":
        with sessions.begin() as db:
            claimed = JobRepository(db).claim(worker_id="original")
            token = claimed.fencing_token

    def explode(*args, **kwargs):
        if stolen:
            with sessions.begin() as db:
                current = db.get(Job, job_id)
                current.leased_by = "replacement"
                current.fencing_token += 1
        raise RuntimeError("private-dispatch-canary")

    monkeypatch.setattr(worker, "_run_claimed_job", explode)
    if entry == "next":
        assert worker.process_next_run_job(sessions, worker_id="original") is False
    else:
        assert worker.process_claimed_run_fixture(
            sessions, job_id=job_id, payload_ref=run_id,
            worker_id="original", token=token,
        ) is False
    with sessions() as db:
        stored = db.get(Job, job_id)
        assert stored.status == ("running" if stolen else "queued")
        assert stored.leased_by == ("replacement" if stolen else None)
        assert stored.fencing_token == (2 if stolen else 1)
        assert "private-dispatch-canary" not in (stored.error or "")
        assert db.get(RunRecord, run_id).status == "queued"


def test_fixture_run_publishes_running_before_fixture_and_evaluating_before_evaluator(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id

    observed = []
    original_fixture = __import__("p2r_orchestration.run_worker", fromlist=["run_fixture"]).run_fixture
    original_evaluator = __import__("p2r_orchestration.run_worker", fromlist=["evaluate_predictions"]).evaluate_predictions

    def fixture(**kwargs):
        with sessions() as db:
            current = db.get(RunRecord, run_id)
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
            observed.append(("fixture", current.status, execution.status, execution.started_at is not None))
        return original_fixture(**kwargs)

    def evaluator(predictions):
        with sessions() as db:
            current = db.get(RunRecord, run_id)
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
            observed.append(("evaluator", current.status, execution.status, execution.started_at is not None))
        return original_evaluator(predictions)

    monkeypatch.setattr("p2r_orchestration.run_worker.run_fixture", fixture)
    monkeypatch.setattr("p2r_orchestration.run_worker.evaluate_predictions", evaluator)
    assert process_next_run_job(sessions) is True
    assert observed == [
        ("fixture", "running", "running", True),
        ("evaluator", "evaluating", "running", True),
    ]
    with sessions() as db:
        assert db.get(RunRecord, run_id).status == "completed"
        assert db.get(Job, job_id).status == "succeeded"


def test_fixture_run_cancellation_is_visible_to_fixture_and_terminal(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id = run.id

    original_fixture = __import__("p2r_orchestration.run_worker", fromlist=["run_fixture"]).run_fixture

    def fixture(**kwargs):
        with sessions.begin() as db:
            db.get(RunRecord, run_id).status = "cancel_requested"
        return original_fixture(**kwargs)

    monkeypatch.setattr("p2r_orchestration.run_worker.run_fixture", fixture)
    assert process_next_run_job(sessions) is True
    with sessions() as db:
        run = db.get(RunRecord, run_id)
        execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
        assert run.status == "cancelled"
        assert execution.status == "cancelled"
        assert db.get(Job, job.id).status == "cancelled"


def test_fixture_execution_lease_is_live_during_external_work(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import p2r_orchestration.run_worker as worker
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id = run.id
    original = worker.run_fixture
    observed = []
    def fixture(**kwargs):
        with sessions() as db:
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
            observed.append(execution.lease_until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        return original(**kwargs)
    monkeypatch.setattr(worker, "run_fixture", fixture)
    assert process_next_run_job(sessions)
    assert observed == [True]


def test_cancel_before_start_rolls_back_when_completion_fence_fails(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db, status="cancel_requested")
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id
    monkeypatch.setattr(JobRepository, "complete", lambda *args, **kwargs: False)
    assert process_next_run_job(sessions) is False
    with sessions() as db:
        assert db.get(RunRecord, run_id).status == "cancel_requested"
        assert db.get(Job, job_id).status == "queued"
        assert len(db.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id)).all()) == 1


def test_requeued_fixture_cancel_finishes_previous_execution(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id
    assert process_next_run_job(sessions, heartbeat=lambda **kwargs: False) is False
    with sessions.begin() as db:
        db.get(RunRecord, run_id).status = "cancel_requested"
    assert process_next_run_job(sessions)
    with sessions() as db:
        assert db.get(RunRecord, run_id).status == "cancelled"
        execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id))
        assert execution.status == "cancelled"
        assert execution.finished_at is not None
        assert execution.lease_until is None
        assert db.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is None


def test_requeued_fixture_reuses_attempt_and_completes_once(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db)
        job = JobRepository(db).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id
    assert process_next_run_job(sessions, heartbeat=lambda **kwargs: False) is False
    assert process_next_run_job(sessions, worker_id="replacement") is True
    with sessions() as db:
        assert db.get(RunRecord, run_id).status == "completed"
        assert len(db.scalars(select(AttemptRecord).where(AttemptRecord.run_id == run_id)).all()) == 1
        assert len(db.scalars(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)).all()) == 1
        executions = db.scalars(select(ExecutionRecord).where(ExecutionRecord.job_id == job_id)).all()
        assert len(executions) == 1
        assert executions[0].fencing_token == 2
        assert executions[0].status == "succeeded"
        terminals = db.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.type == "run.terminal")).all()
        assert len(terminals) == 1


def test_fixture_job_completes_run_evaluation_report_and_events_once(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id = run.id
    assert process_next_run_job(sessions)
    assert not process_next_run_job(sessions)
    with sessions.begin() as session:
        JobRepository(session).enqueue(kind="run_fixture", payload_ref=run_id)
    assert process_next_run_job(sessions)
    with sessions() as session:
        stored = session.get(RunRecord, run_id)
        assert stored.status == "completed"
        assert session.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is not None
        assert session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is None
        assert session.get(Job, job.id).status == "succeeded"
        assert len(session.scalars(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)).all()) == 1
        assert len(session.scalars(select(ReportRecord).where(ReportRecord.run_id == run_id)).all()) == 0
        assert stored.report_hash is None
        events = session.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id).order_by(RunEventRecord.seq)).all()
        assert [event.seq for event in events] == [0, 1, 2, 3]
        assert [event.type for event in events] == ['run.created', 'stage.changed', 'stage.changed', 'run.terminal']
        assert [event.payload_json['status'] for event in events] == ['queued', 'running', 'evaluating', 'completed']


def test_run_worker_rolls_back_run_business_state_when_fenced_completion_fails(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id = run.id

    monkeypatch.setattr(JobRepository, "complete", lambda self, job_id, *, worker_id, fencing_token=None: False)
    assert process_next_run_job(sessions) is False

    with sessions() as session:
        stored = session.get(RunRecord, run_id)
        assert stored.status == "evaluating"
        assert session.scalar(select(AttemptRecord).where(AttemptRecord.run_id == run_id)) is not None
        assert session.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is None
        assert session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is None
        assert session.get(Job, job.id).status == "queued"


def test_fixture_job_heartbeat_false_rolls_back_business_state_and_requeues(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id, job_id = run.id, job.id

    seen = []
    def heartbeat(*, job_id, worker_id, fencing_token):
        seen.append((job_id, worker_id, fencing_token))
        return False

    assert process_next_run_job(sessions, worker_id="heartbeat-worker", heartbeat=heartbeat) is False

    with sessions() as session:
        stored = session.get(RunRecord, run_id)
        assert stored.status == "evaluating"
        assert session.scalar(select(AttemptRecord).where(AttemptRecord.run_id == run_id)) is not None
        assert session.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is None
        assert session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is None
        stored_job = session.get(Job, job_id)
        assert stored_job.status == "queued"
        assert seen == [(job_id, "heartbeat-worker", 1)]




def test_fixture_job_heartbeat_receives_current_token_and_still_completes(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id, job_id = run.id, job.id

    seen = []
    def heartbeat(*, job_id, worker_id, fencing_token):
        seen.append((job_id, worker_id, fencing_token))
        return True

    assert process_next_run_job(sessions, worker_id="heartbeat-worker", heartbeat=heartbeat) is True
    with sessions() as session:
        stored_job = session.get(Job, job_id)
        assert seen == [(job_id, "heartbeat-worker", 1)]
        assert session.get(RunRecord, run_id).status == "completed"
        assert stored_job.status == "succeeded"


def test_fixture_job_records_initial_attempt_for_run(tmp_path):

    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id = run.id

    assert process_next_run_job(sessions)
    with sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(AttemptRecord.run_id == run_id)).all()
        assert len(attempts) == 1
        assert attempts[0].attempt_index == 0
        assert attempts[0].source == "template"
        assert attempts[0].status == "validated"




def test_repair_attempt_job_creates_new_attempt_and_completes(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="failed")
        base = AttemptRecord(run_id=run.id, attempt_index=0, source="template", status="rejected", base_code_hash=run.manifest_hash, validation_json={}, code_manifest_json={})
        session.add(base); session.flush()
        session.add(RepairProposalRecord(run_id=run.id, attempt_id=base.id, repair_ordinal=1, base_code_hash=run.manifest_hash, patch_hash="f" * 64, proposal_json={"source": "repair"}, status="accepted"))
        session.flush()
        proposal = session.scalar(select(RepairProposalRecord).where(RepairProposalRecord.run_id == run.id))
        job = JobRepository(session).enqueue(kind="repair_attempt", payload_ref=proposal.id, idempotency_key="repair:" + proposal.id)
        run_id, proposal_id = run.id, proposal.id
    assert process_next_run_job(sessions)
    with sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(AttemptRecord.run_id == run_id).order_by(AttemptRecord.attempt_index)).all()
        assert len(attempts) == 2
        assert attempts[1].attempt_index == 1
        assert attempts[1].source == "repair"
        assert attempts[1].repair_ordinal == 1
        assert attempts[1].status == "validated"
        assert session.get(Job, job.id).status == "succeeded"
        assert session.get(RepairProposalRecord, proposal_id).status == "accepted"


def test_repair_attempt_job_rolls_back_attempt_when_fenced_completion_fails(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="failed")
        base = AttemptRecord(run_id=run.id, attempt_index=0, source="template", status="rejected", base_code_hash=run.manifest_hash, validation_json={}, code_manifest_json={})
        session.add(base); session.flush()
        proposal = RepairProposalRecord(run_id=run.id, attempt_id=base.id, repair_ordinal=1, base_code_hash=run.manifest_hash, patch_hash="f" * 64, proposal_json={"source": "repair"}, status="accepted")
        session.add(proposal); session.flush()
        job = JobRepository(session).enqueue(kind="repair_attempt", payload_ref=proposal.id, idempotency_key="repair:" + proposal.id)
        run_id, proposal_id, job_id = run.id, proposal.id, job.id

    monkeypatch.setattr(JobRepository, "complete", lambda self, job_id, *, worker_id, fencing_token=None: False)
    assert process_next_run_job(sessions) is False

    with sessions() as session:
        assert session.scalars(select(AttemptRecord).where(AttemptRecord.run_id == run_id)).all().__len__() == 1
        assert session.get(RepairProposalRecord, proposal_id).status == "accepted"
        assert session.get(Job, job_id).status == "queued"


def test_repair_attempt_job_is_idempotent_for_existing_attempt(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="failed")
        base = AttemptRecord(run_id=run.id, attempt_index=0, source="template", status="rejected", base_code_hash=run.manifest_hash, validation_json={}, code_manifest_json={})
        session.add(base); session.flush()
        session.add(RepairProposalRecord(run_id=run.id, attempt_id=base.id, repair_ordinal=1, base_code_hash=run.manifest_hash, patch_hash="f" * 64, proposal_json={"source": "repair"}, status="accepted"))
        session.flush()
        proposal = session.scalar(select(RepairProposalRecord).where(RepairProposalRecord.run_id == run.id))
        session.add(AttemptRecord(run_id=run.id, attempt_index=1, source="repair", repair_ordinal=1, status="validated", base_code_hash=run.manifest_hash, patch_hash="f" * 64, validation_json={}, code_manifest_json={}))
        session.flush()
        job = JobRepository(session).enqueue(kind="repair_attempt", payload_ref=proposal.id)
    assert process_next_run_job(sessions)
    with sessions() as session:
        assert len(session.scalars(select(AttemptRecord).where(AttemptRecord.run_id == run.id)).all()) == 2
        assert session.get(Job, job.id).status == "succeeded"


def test_fixture_runner_unexpected_error_fails_job_and_run(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id, job_id = run.id, job.id

    def explode(**kwargs):
        raise RuntimeError("unexpected fixture failure")

    monkeypatch.setattr("p2r_orchestration.run_worker.run_fixture", explode)
    assert process_next_run_job(sessions) is True

    with sessions() as session:
        run_record = session.get(RunRecord, run_id)
        assert run_record.status == "failed"
        assert run_record.error_json["code"] == "PIPELINE_FAILED"
        assert session.get(Job, job_id).status == "failed"
        # Public Run/Execution/Job failure surfaces carry the fixed code only;
        # raw runner exception text must never be persisted.
        execution = session.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
        job_record = session.get(Job, job_id)
        for surface in (run_record.error_json, execution.error_json, job_record.error):
            assert "unexpected fixture failure" not in (surface if isinstance(surface, str) else json.dumps(surface))
        assert execution.error_json["code"] == "PIPELINE_FAILED"
        assert job_record.error == "PIPELINE_FAILED"


def test_legacy_export_report_json_branch_uses_fixed_public_error(tmp_path):
    """The retained export_report_json path persists EXPORT_FAILED only."""
    from p2r_orchestration.run_worker import _run_claimed_job

    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="completed")
        report = ReportRecord(run_id=run.id, content_json={"ok": True}, content_hash="f" * 64)
        session.add(report); session.flush()
        from p2r_persistence.models import ExportRecord
        export = ExportRecord(
            run_id=run.id,
            report_hash=report.content_hash,
            requested_formats_json=["report_json"],
            items_json=[{"format": "report_json", "status": "pending"}],
        )
        session.add(export); session.flush()
        JobRepository(session).enqueue(kind="export_report_json", payload_ref=export.id)
        claimed = JobRepository(session).claim(worker_id="legacy-export-worker", kinds=("export_report_json",))
        assert claimed is not None
        export_id, job_id, token = export.id, claimed.id, claimed.fencing_token

    assert _run_claimed_job(
        sessions, job_id=job_id, payload_ref=export_id, kind="export_report_json",
        worker_id="legacy-export-worker", token=token,
    ) is True

    with sessions() as session:
        job_record = session.get(Job, job_id)
        assert job_record.status == "failed"
        assert job_record.error == "EXPORT_FAILED"
        export = session.get(ExportRecord, export_id)
        assert export.status == "failed"
        assert export.error_json["code"] == "EXPORT_FAILED"
        assert "artifact_store" not in (job_record.error or "")


def test_export_artifact_error_fails_export_job_without_leaving_running(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="completed")
        report = ReportRecord(run_id=run.id, content_json={"ok": True}, content_hash="f" * 64)
        session.add(report); session.flush()
        from p2r_persistence.models import ExportItemRecord, ExportRecord
        export = ExportRecord(
            run_id=run.id,
            report_hash=report.content_hash,
            requested_formats_json=["report_json"],
            items_json=[{"format": "report_json", "status": "pending"}],
        )
        session.add(export); session.flush()
        session.add(ExportItemRecord(export_id=export.id, format="report_json", status="pending"))
        job = JobRepository(session).enqueue(kind="export_report_json", payload_ref=export.id)
        export_id, job_id = export.id, job.id

    secret_canary = "SECRET-backend-detail-c8f19a"
    class BrokenStore:
        def put_bytes(self, data):
            raise OSError(secret_canary)
        def open_bytes(self, sha256):
            raise OSError(secret_canary)

    assert process_next_run_job(sessions, artifact_store=BrokenStore()) is True

    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed"
        # Public job failures stay a fixed safe message; raw backend exception
        # text must never leak into persisted job/export error surfaces.
        assert job.error == "export failed"
        assert secret_canary not in (job.error or "")
        export = session.get(ExportRecord, export_id)
        assert export.status == "failed"
        assert export.artifact_id is None
        assert all(item.get("error", {}).get("code") != secret_canary for item in export.items_json)


def test_unsupported_job_kind_checks_fenced_failure(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        job = JobRepository(session).enqueue(kind="unsupported", payload_ref="x")
        job_id = job.id

    monkeypatch.setattr(JobRepository, "fail", lambda self, *args, **kwargs: False)
    assert process_next_run_job(sessions) is False
    with sessions() as session:
        assert session.get(Job, job_id).status == "queued"


@pytest.mark.parametrize("terminal_status", ["failed", "awaiting_revision"])
def test_terminal_run_duplicate_job_is_completed_without_rerunning(tmp_path, monkeypatch, terminal_status):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status=terminal_status)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id

    def unexpected_run(**kwargs):
        raise AssertionError("terminal run was rerun")

    monkeypatch.setattr("p2r_orchestration.run_worker.run_fixture", unexpected_run)
    assert process_next_run_job(sessions) is True

    with sessions() as session:
        assert session.get(RunRecord, run_id).status == terminal_status
        assert session.get(Job, job_id).status == "succeeded"
        assert session.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is None
        assert session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is None


def test_terminal_duplicate_job_rolls_back_when_fenced_completion_fails(tmp_path, monkeypatch):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="failed")
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id

    monkeypatch.setattr(JobRepository, "complete", lambda self, job_id, *, worker_id, fencing_token=None: False)
    assert process_next_run_job(sessions) is False

    with sessions() as session:
        assert session.get(RunRecord, run_id).status == "failed"
        assert session.get(Job, job_id).status == "queued"


def test_awaiting_revision_is_a_valid_terminal_run_state(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="awaiting_revision")
        run_id = run.id
    with sessions() as session:
        assert session.get(RunRecord, run_id).status == "awaiting_revision"


def test_cancel_requested_job_becomes_cancelled_without_evaluation_or_report(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session, status="cancel_requested")
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id, idempotency_key="run:" + run.id)
        run_id = run.id
    assert process_next_run_job(sessions)
    with sessions() as session:
        assert session.get(RunRecord, run_id).status == "cancelled"
        assert session.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is None
        assert session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is None
        assert session.get(Job, job.id).status == "succeeded"
        assert session.scalar(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.type == "run.cancelled")) is not None


def test_run_with_store_registers_real_runner_output_as_evidence(tmp_path):
    """The controlled run must register its real output; store-less runs don't."""
    import hashlib

    from p2r_artifacts.store import LocalArtifactStore
    from p2r_orchestration.evidence_registry import EVIDENCE_EVENT_TYPE
    from p2r_orchestration.export_builder import canonical_json
    from p2r_orchestration.run_worker import process_claimed_run_fixture
    from p2r_runner import run_fixture

    store = LocalArtifactStore(tmp_path / "artifacts")
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        job = JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id, job_id = run.id, job.id
        claimed = JobRepository(session).claim(worker_id="evidence-worker")
        assert claimed is not None
        token = claimed.fencing_token
    assert process_next_run_job(sessions) is False
    assert process_claimed_run_fixture(
        sessions, job_id=job_id, payload_ref=run_id,
        worker_id="evidence-worker", token=token,
        artifact_store=store,
    )
    with sessions() as session:
        assert session.get(RunRecord, run_id).status == "completed"
        event = session.scalar(select(RunEventRecord).where(
            RunEventRecord.run_id == run_id, RunEventRecord.type == EVIDENCE_EVENT_TYPE))
        assert event is not None
    expected = canonical_json(run_fixture(recipe_id="offline_fixture", recipe_version="1"))
    assert event.payload_json["evidence"] == [{
        "run_id": run_id,
        "sha256": hashlib.sha256(expected).hexdigest(),
        "bytes": len(expected),
        "media_type": "application/json",
    }]


def test_run_without_store_completes_without_evidence_registration(tmp_path):
    sessions = factory(tmp_path)
    with sessions.begin() as session:
        run = make_run(session)
        JobRepository(session).enqueue(kind="run_fixture", payload_ref=run.id)
        run_id = run.id
    assert process_next_run_job(sessions)
    with sessions() as session:
        assert session.get(RunRecord, run_id).status == "completed"
        assert session.scalar(select(RunEventRecord).where(
            RunEventRecord.run_id == run_id,
            RunEventRecord.type == "run.evidence.registered")) is None
