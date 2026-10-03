import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app
from p2r_artifacts.store import ArtifactIntegrityError, LocalArtifactStore
from p2r_orchestration.run_worker import process_next_run_job
from p2r_orchestration.report_jobs import process_next_report_job
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import EnvironmentCheckRecord, ExportRecord, Job, RunRecord


def _headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


@pytest.fixture
def seeded(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    address = store.put_bytes(b'Controlled smoke teaching fixture. Not a full reproduction.')
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'export.sqlite3'}",
        supervisor=object(), secure_cookies=False, artifact_store=store,
    )
    factory = app.state.runtime["session_factory"]
    from p2r_persistence.models import (
        ExtractionRecord, MembershipRecord, PaperRecord, ProjectRecord, UserRecord,
    )
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        project = ProjectRecord(name="export project", created_by=user.id)
        other = ProjectRecord(name="other project", created_by=user.id)
        db.add(user); db.flush()
        db.add_all([project, other]); db.flush()
        db.add_all([
            MembershipRecord(project_id=project.id, user_id=user.id, role="owner"),
            MembershipRecord(project_id=other.id, user_id=user.id, role="owner"),
        ])
        paper = PaperRecord(project_id=project.id, title="fixture", status="ready", sha256=address.sha256, artifact_key=address.sha256, bytes=address.bytes, page_count=0, source_kind="text_fixture", media_type="text/plain")
        db.add(paper); db.flush()
        extraction = ExtractionRecord(paper_id=paper.id, content_sha256=address.sha256, payload_json={"candidate_experiments": [{"id": "candidate-1", "recipe_id": "offline_fixture", "recipe_version": "1", "label": "controlled smoke", "execution_support": "supported"}]})
        db.add(extraction); db.flush()
        ids = {"project": project.id, "other": other.id, "paper": paper.id, "extraction": extraction.id}
    return app, ids


def _prepare_report(client, app, run_id):
    response = client.post(f"/api/v1/runs/{run_id}/report-builds", headers=_headers("report-build"), json={})
    assert response.status_code == 202, response.text
    assert process_next_report_job(app.state.runtime["session_factory"])
    assert client.get(f"/api/v1/runs/{run_id}/report-status", headers=_headers("read")).json()["data"]["status"] == "ready"


def _prepare_run(app, client, ids):
    experiment = client.post(
        f"/api/v1/projects/{ids['project']}/experiments",
        headers=_headers("experiment"),
        json={"paper_id": ids["paper"], "extraction_id": ids["extraction"], "candidate_experiment_id": "candidate-1", "recipe_id": "offline_fixture", "recipe_version": "1"},
    ).json()["data"]
    revision = client.post(
        f"/api/v1/projects/{ids['project']}/spec-revisions",
        headers=_headers("revision"),
        json={"experiment_id": experiment["experiment_id"], "base_revision_id": None, "spec": {"schema_version": "1.0", "target": "fixture"}},
    ).json()["data"]
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        db.add(EnvironmentCheckRecord(
            project_id=ids["project"],
            experiment_id=experiment["experiment_id"],
            revision_id=revision["id"],
            decision="ready",
            checker_version="test",
            requirements_json={},
            items_json=[],
            suggestions_json=[],
        ))
    confirmation = client.post(
        f"/api/v1/spec-revisions/{revision['id']}/confirm",
        headers=_headers("confirmation"),
        json={"spec_hash": revision["spec_hash"], "policy_hash": revision["policy_hash"], "accepted_issue_codes": [], "accept_budget": True},
    ).json()["data"]
    created = client.post(
        f"/api/v1/projects/{ids['project']}/runs",
        headers=_headers("run"),
        json={"revision_id": revision["id"], "confirmation_id": confirmation["confirmation_id"], "mode": "local-cpu"},
    ).json()["data"]
    return created["run_id"]


def test_export_worker_persists_content_addressed_artifact_and_downloads(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        queued = client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("export"), json={"formats": ["report_json"]})
        assert queued.status_code == 202
        assert process_next_run_job(app.state.runtime["session_factory"], app.state.runtime["artifact_store"])
        exports = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"})
        item = exports.json()["data"][0]
        assert item["status"] == "succeeded"
        artifact_id = item["artifact_id"]
        download = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers={"X-Actor-ID": "owner"})
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == artifact_id
        assert json.loads(download.content)["run_id"] == run_id


def test_export_worker_fences_completion_after_lease_changes(seeded, monkeypatch):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        assert client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("export-stale"),
            json={"formats": ["report_json"]},
        ).status_code == 202

        factory = app.state.runtime["session_factory"]
        with factory.begin() as db:
            job = db.scalar(select(Job).where(Job.kind == "export_report_json"))
            export_id = job.payload_ref
            original = JobRepository(db).claim(worker_id="original")
            assert original is not None
            old_token = original.fencing_token
            original.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert JobRepository(db).requeue_expired() == 1
            replacement = JobRepository(db).claim(worker_id="replacement")
            assert replacement.fencing_token == old_token + 1
            replacement.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert JobRepository(db).requeue_expired() == 1

        original_claim = JobRepository.claim
        monkeypatch.setattr(
            JobRepository,
            "claim",
            lambda self, *, worker_id, lease_seconds=60, kinds=None: original_claim(
                self, worker_id=worker_id, lease_seconds=lease_seconds, kinds=kinds
            ),
        )
        assert process_next_run_job(factory, app.state.runtime["artifact_store"])
        with factory() as db:
            job = db.get(Job, job.id)
            export = db.get(ExportRecord, export_id)
            assert job.status == "succeeded"
            assert export.status == "succeeded"


def test_export_worker_rolls_back_business_write_when_fenced_completion_fails(seeded, monkeypatch):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        assert client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("export-fenced-write"),
            json={"formats": ["report_json"]},
        ).status_code == 202

        factory = app.state.runtime["session_factory"]
        with factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "export_report_json"))
            export_id = job.payload_ref

        def reject_completion(self, job_id, *, worker_id, fencing_token=None):
            return False

        monkeypatch.setattr(JobRepository, "complete", reject_completion)
        assert process_next_run_job(factory, app.state.runtime["artifact_store"]) is False

        with factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "export_report_json"))
            export = db.get(ExportRecord, export_id)
            assert job.status == "queued"
            assert export.status == "queued"
            assert export.artifact_id is None
            assert export.storage_key is None


@pytest.mark.parametrize("failure", ["missing_report", "missing_store"])
def test_export_worker_rolls_back_failure_state_when_fenced(seeded, monkeypatch, failure):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        factory = app.state.runtime["session_factory"]
        assert process_next_run_job(factory)
        _prepare_report(client, app, run_id)
        assert client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("export-fenced-failure"),
            json={"formats": ["report_json"]},
        ).status_code == 202
        if failure == "missing_report":
            from p2r_persistence.models import ReportRecord
            with factory.begin() as db:
                db.delete(db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id)))
        monkeypatch.setattr(JobRepository, "fail", lambda self, *args, **kwargs: False)
        store = app.state.runtime["artifact_store"] if failure == "missing_report" else None
        assert process_next_run_job(factory, store) is False
        with factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "export_report_json"))
            export = db.get(ExportRecord, job.payload_ref)
            assert job.status == "queued"
            assert export.status == "queued"
            assert not export.error_json


def test_artifact_download_rejects_metadata_mismatch_and_corrupt_bytes(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        assert client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("export"), json={"formats": ["report_json"]}).status_code == 202
        assert process_next_run_job(app.state.runtime["session_factory"], app.state.runtime["artifact_store"])
        artifact_id = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"}).json()["data"][0]["artifact_id"]
        with app.state.runtime["session_factory"].begin() as db:
            export = db.scalar(select(ExportRecord).where(ExportRecord.artifact_id == artifact_id))
            export.sha256 = "0" * 64
        mismatch = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers={"X-Actor-ID": "owner"})
        assert mismatch.status_code == 409
        with app.state.runtime["session_factory"].begin() as db:
            export = db.scalar(select(ExportRecord).where(ExportRecord.artifact_id == artifact_id))
            export.sha256 = artifact_id
        path = app.state.runtime["artifact_store"]._path(artifact_id)
        path.write_bytes(b"corrupt")
        corrupt = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers={"X-Actor-ID": "owner"})
        assert corrupt.status_code == 409


def test_artifact_download_hides_cross_project_and_missing_artifacts(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        assert client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("export"), json={"formats": ["report_json"]}).status_code == 202
        assert process_next_run_job(app.state.runtime["session_factory"], app.state.runtime["artifact_store"])
        artifact_id = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"}).json()["data"][0]["artifact_id"]
        with app.state.runtime["session_factory"].begin() as db:
            from p2r_persistence.models import MembershipRecord
            db.delete(db.scalar(select(MembershipRecord).where(MembershipRecord.project_id == ids["project"], MembershipRecord.user_id == "owner")))
        hidden = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers={"X-Actor-ID": "owner"})
        assert hidden.status_code == 404
        missing = client.get(f"/api/v1/artifacts/{'f' * 64}/download", headers={"X-Actor-ID": "owner"})
        assert missing.status_code == 404
