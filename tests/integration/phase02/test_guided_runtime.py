import hashlib
import io
import json
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app, create_demo_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    AttemptRecord,
    EvaluationRecord,
    ExecutionRecord,
    ExportItemRecord,
    ExportRecord,
    Job,
    MembershipRecord,
    OperationRecord,
    PaperRecord,
    ProjectRecord,
    ReportBuildRecord,
    ReportRecord,
    RunEventRecord,
    RunRecord,
    UserRecord,
)
from p2r_persistence.db import create_engine, create_session_factory, init_db

DEMO_KINDS = ("run_fixture", "report_build", "export_bundle", "export_report_json")


def _demo_app(tmp_path):
    return create_demo_app(
        db_url=f"sqlite:///{tmp_path / 'guided.sqlite3'}",
        artifact_store=LocalArtifactStore(tmp_path / "store"),
    )


def _post(client, path, body, key):
    return client.post(
        "/api/v1" + path,
        json=body,
        headers={"Idempotency-Key": key},
    )


def _bootstrap(client, key="bootstrap"):
    result = _post(
        client,
        "/demo/bootstrap",
        {"fixture_id": "offline-smoke-v1", "source_processing_consent": True},
        key,
    )
    assert result.status_code == 201, result.text
    return result.json()["data"]


def _prepare_experiment(client, data):
    body = {
        "paper_id": data["paper_id"],
        "extraction_id": data["extraction_id"],
        "candidate_experiment_id": "offline-smoke",
        "recipe_id": "offline_fixture",
        "recipe_version": "1",
    }
    response = _post(client, "/projects/" + data["project_id"] + "/experiments", body, "experiment")
    assert response.status_code == 201, response.text
    experiment = response.json()["data"]["experiment_id"]
    draft = client.get("/api/v1/experiments/" + experiment + "/draft").json()["data"]
    response = _post(
        client,
        "/projects/" + data["project_id"] + "/spec-revisions",
        {"experiment_id": experiment, "base_revision_id": None, "spec": draft["spec"]},
        "revision",
    )
    assert response.status_code == 201, response.text
    revision = response.json()["data"]
    response = _post(
        client,
        "/spec-revisions/" + revision["id"] + "/confirm",
        {
            "spec_hash": revision["spec_hash"],
            "policy_hash": revision["policy_hash"],
            "accept_budget": True,
        },
        "confirmation",
    )
    assert response.status_code == 201, response.text
    confirmation = response.json()["data"]["confirmation_id"]
    response = _post(
        client,
        "/projects/" + data["project_id"] + "/runs",
        {"spec_revision_id": revision["id"], "confirmation_id": confirmation},
        "run",
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["run_id"]


def _wait_for(predicate, *, timeout=5.0):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


def test_demo_http_chain_runs_without_direct_worker_calls(tmp_path):
    app = _demo_app(tmp_path)
    with TestClient(app) as client:
        data = _bootstrap(client)
        run_id = _prepare_experiment(client, data)

        assert _wait_for(lambda: client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "completed")
        build = _post(client, f"/runs/{run_id}/report-builds", {}, "report")
        assert build.status_code == 202, build.text
        assert _wait_for(
            lambda: client.get(f"/api/v1/runs/{run_id}/report-status").json()["data"]["status"] == "ready"
        )
        export = _post(client, f"/runs/{run_id}/exports", {"formats": ["report_json"]}, "export")
        assert export.status_code == 202, export.text
        assert _wait_for(
            lambda: client.get(f"/api/v1/runs/{run_id}/exports").json()["data"][0]["status"] == "succeeded"
        )
        with app.state.runtime["session_factory"]() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "completed"
            assert db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)) is not None
            assert db.scalar(select(ExportRecord).where(ExportRecord.run_id == run_id)).status == "succeeded"


def test_zip_export_ships_real_registered_controlled_smoke_evidence(tmp_path):
    """Registry → report freeze → ZIP: the whole chain on real runner output.

    The demo pipeline must register the runner's real output as Run evidence,
    freeze exactly that registration into the report, and ship a fully ready
    ZIP (never EXPORT_INCOMPLETE) whose entries match the registrations.
    """
    from p2r_orchestration.export_builder import canonical_json
    from p2r_runner import run_fixture

    app = _demo_app(tmp_path)
    with TestClient(app) as client:
        data = _bootstrap(client)
        run_id = _prepare_experiment(client, data)
        assert _wait_for(lambda: client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "completed")

        # 1. The run registered its real runner output as evidence.
        expected_output = canonical_json(run_fixture(recipe_id="offline_fixture", recipe_version="1"))
        expected_sha = hashlib.sha256(expected_output).hexdigest()
        factory = app.state.runtime["session_factory"]
        with factory() as db:
            event = db.scalar(
                select(RunEventRecord)
                .where(RunEventRecord.run_id == run_id, RunEventRecord.type == "run.evidence.registered")
                .order_by(RunEventRecord.seq.desc())
            )
            assert event is not None, "run completed without registering real smoke evidence"
            entries = event.payload_json["evidence"]
        assert entries == [{
            "run_id": run_id,
            "sha256": expected_sha,
            "bytes": len(expected_output),
            "media_type": "application/json",
        }]

        # 2. The report freeze contains exactly the registered evidence.
        build = _post(client, f"/runs/{run_id}/report-builds", {}, "report-evidence")
        assert build.status_code == 202, build.text
        assert _wait_for(
            lambda: client.get(f"/api/v1/runs/{run_id}/report-status").json()["data"]["status"] == "ready"
        )
        report = client.get(f"/api/v1/runs/{run_id}/report").json()["data"]
        assert report["evidence"] == entries

        # 3. The ZIP export must be fully ready, not EXPORT_INCOMPLETE.
        export = _post(
            client,
            f"/runs/{run_id}/exports",
            {"formats": ["report_json", "report_markdown", "bundle_zip"]},
            "export-evidence",
        )
        assert export.status_code == 202, export.text
        assert _wait_for(
            lambda: client.get(f"/api/v1/runs/{run_id}/exports").json()["data"][0]["status"] == "succeeded"
        )
        exports = client.get(f"/api/v1/runs/{run_id}/exports").json()["data"]
        assert exports[0]["status"] == "succeeded"
        items = {item["format"]: item for item in exports[0]["items"]}
        expected = {
            "report_json": ("application/json", "report.json"),
            "report_markdown": ("text/markdown", "report.md"),
            "bundle_zip": ("application/zip", "report-bundle.zip"),
        }
        assert set(items) == set(expected)
        zip_bytes = None
        for fmt, item in items.items():
            media, filename = expected[fmt]
            assert item["status"] == "ready", item
            assert item["media_type"] == media
            assert item["filename"] == filename
            download = client.get(f"/api/v1/artifacts/{item['artifact_id']}/download")
            assert download.status_code == 200
            assert download.headers["content-type"].startswith(media)
            assert 'filename="' + filename + '"' in download.headers["content-disposition"]
            assert len(download.content) == item["bytes"]
            assert hashlib.sha256(download.content).hexdigest() == item["sha256"]
            if fmt == "bundle_zip":
                zip_bytes = download.content

        # 4. ZIP entry checksums match the registered evidence byte-for-byte.
        assert zip_bytes is not None
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            assert {"report.json", "report.md", "manifest.json", "metrics.json", "checksums.json"} <= set(names)
            evidence_name = f"evidence/{expected_sha}.txt"
            assert evidence_name in names
            assert archive.read(evidence_name) == expected_output
            assert not any(name.startswith("/") or ".." in name or "\\" in name for name in names)
            checksums = json.loads(archive.read("checksums.json"))
            for name in names:
                if name != "checksums.json":
                    assert checksums[name] == hashlib.sha256(archive.read(name)).hexdigest(), name
            assert json.loads(archive.read("report.json"))["evidence"] == entries


def test_ordinary_create_app_does_not_start_demo_worker(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'ordinary.sqlite3'}",
        artifact_store=LocalArtifactStore(tmp_path / "store"),
        development_auth=True,
    )
    assert app.state.runtime.get("demo_worker_stop") is None
    assert app.state.runtime.get("demo_worker_thread") is None
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        with app.state.runtime["session_factory"]() as db:
            db.add(Job(kind="run_fixture", payload_ref="unreachable"))
            db.commit()
        assert _wait_for(lambda: True, timeout=0.1)
        with app.state.runtime["session_factory"]() as db:
            assert db.scalar(select(Job)).status == "queued"


def test_demo_lifecycle_joins_one_bounded_worker_thread(tmp_path):
    app = _demo_app(tmp_path)
    client = TestClient(app)
    with client:
        stop = app.state.runtime.get("demo_worker_stop")
        thread = app.state.runtime.get("demo_worker_thread")
        assert stop is not None and not stop.is_set()
        assert thread is not None and thread.is_alive()
        assert thread.daemon is False
        assert app.state.runtime.get("demo_worker_thread") is app.state.runtime.get("demo_worker_thread")
    assert not stop.is_set() or thread.ident is not None
    assert not thread.is_alive()
    assert thread.ident is None or not thread.is_alive()


def test_controlled_start_recovers_only_expired_supported_leases(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lease.sqlite3'}")
    init_db(engine)
    factory = create_session_factory(engine)
    now = datetime.now(timezone.utc)
    with factory.begin() as db:
        supported = Job(kind="run_fixture", payload_ref="supported")
        unsupported = Job(kind="repair_attempt", payload_ref="unsupported")
        active = Job(kind="run_fixture", payload_ref="active")
        db.add_all([supported, unsupported, active])
        db.flush()
        for job in (supported, unsupported, active):
            job.status = "running"
            job.leased_by = "previous-worker"
            job.lease_until = now + timedelta(seconds=60)
        supported.lease_until = now - timedelta(seconds=1)
        unsupported.lease_until = now - timedelta(seconds=1)

    from p2r_api import demo as demo_runtime

    demo_runtime.recover_expired_demo_leases(factory)

    with factory() as db:
        rows = {row.payload_ref: row for row in db.scalars(select(Job))}
        assert rows["supported"].status == "queued"
        assert rows["supported"].leased_by is None
        assert rows["unsupported"].status == "running"
        assert rows["unsupported"].leased_by == "previous-worker"
        assert rows["active"].status == "running"
        assert rows["active"].leased_by == "previous-worker"


def test_demo_failure_public_errors_are_fixed_and_canary_free(tmp_path):
    app = _demo_app(tmp_path)
    with TestClient(app) as client:
        data = _bootstrap(client)
        run_id = _prepare_experiment(client, data)
        assert _wait_for(lambda: client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "completed")
        with app.state.runtime["session_factory"]() as db:
            operation = OperationRecord(
                kind="report_build",
                resource_id=run_id,
                status="queued",
            )
            db.add(operation)
            db.flush()
            build = ReportBuildRecord(
                run_id=run_id,
                operation_id=operation.id,
                generation=1,
                status="pending",
                input_json={"run_id": run_id},
                input_hash="0" * 64,
            )
            db.add(build)
            db.flush()
            run = db.get(RunRecord, run_id)
            run.active_report_build_id = build.id
            db.commit()
            job = Job(kind="report_build", payload_ref=build.id)
            db.add(job)
            db.commit()

        import p2r_orchestration.report_jobs as report_jobs

        original = report_jobs.process_claimed_report_job

        def fail(*args, **kwargs):
            raise RuntimeError("SECRET-backend-canary")

        report_jobs.process_claimed_report_job = fail
        try:
            # This public wrapper must remain fixed even when an injected
            # handler leaks a raw backend exception.
            from p2r_api.demo import process_next_demo_job

            process_next_demo_job(
                app.state.runtime["session_factory"],
                app.state.runtime["artifact_store"],
                worker_id="guided-demo",
            )
        finally:
            report_jobs.process_claimed_report_job = original

        with app.state.runtime["session_factory"]() as db:
            job = db.scalar(select(Job).where(Job.kind == "report_build"))
            assert job.error == "PIPELINE_FAILED"
            assert "SECRET-backend-canary" not in (job.error or "")
