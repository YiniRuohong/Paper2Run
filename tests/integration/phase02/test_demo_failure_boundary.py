"""The demo dispatch boundary must terminate business state with its owned Job."""
import pytest
from sqlalchemy import select

from p2r_api.demo import _fail_owned_job
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    ExportRecord, ExportItemRecord, OperationRecord, ReportBuildRecord, RunRecord,
)
from test_run_worker import factory, make_run


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("kind", ["run_fixture", "report_build", "export_bundle"])
def test_owned_dispatch_failure_finishes_business_state(tmp_path, kind, stale):
    sessions = factory(tmp_path)
    with sessions.begin() as db:
        run = make_run(db, status="running" if kind == "run_fixture" else "completed")
        run_id = run.id
        operation = None
        if kind == "report_build":
            operation = OperationRecord(kind=kind, resource_id=run.id, status="running")
            db.add(operation); db.flush()
            record = ReportBuildRecord(run_id=run.id, operation_id=operation.id, generation=1,
                                       status="generating", input_json={}, input_hash="a" * 64)
            db.add(record); db.flush()
            run.active_report_build_id = record.id
        elif kind == "export_bundle":
            operation = OperationRecord(kind=kind, resource_id=run.id, status="running")
            db.add(operation); db.flush()
            record = ExportRecord(run_id=run.id, operation_id=operation.id, status="running",
                                  requested_formats_json=["report_json"], items_json=[])
            db.add(record); db.flush()
            db.add(ExportItemRecord(export_id=record.id, format="report_json", status="running"))
        else:
            record = run
        payload_ref = record.id
        operation_id = operation.id if operation else None
        job = JobRepository(db).enqueue(kind=kind, payload_ref=payload_ref)
        claimed = JobRepository(db).claim(worker_id="boundary", kinds=(kind,))
        job_id, token = job.id, claimed.fencing_token
    _fail_owned_job(sessions, job_id, worker_id="boundary", token=token - 1 if stale else token,
                    kind=kind, payload_ref=payload_ref)
    with sessions() as db:
        cls = {"run_fixture": RunRecord, "report_build": ReportBuildRecord, "export_bundle": ExportRecord}[kind]
        record = db.get(cls, payload_ref)
        if stale:
            assert record.status == ("generating" if kind == "report_build" else "running")
            if operation_id:
                assert db.get(OperationRecord, operation_id).status == "running"
            return
        assert record.status == "failed"
        assert record.error_json["code"] in {"PIPELINE_FAILED", "REPORT_BUILD_FAILED", "EXPORT_FAILED"}
        if operation_id:
            assert db.get(OperationRecord, operation_id).status == "failed"
        if kind == "export_bundle":
            assert db.scalar(select(ExportItemRecord).where(ExportItemRecord.export_id == payload_ref)).status == "failed"
        if kind != "run_fixture":
            assert db.get(RunRecord, run_id).status == "completed"
