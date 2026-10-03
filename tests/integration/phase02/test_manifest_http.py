import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))

from p2r_api.main import create_app
from p2r_persistence.models import (
    AttemptRecord,
    ExecutionRecord,
    ExperimentRecord,
    ExportRecord,
    ExtractionRecord,
    Job,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    RunRecord,
    SpecRevisionRecord,
    UserRecord,
)

H = "a" * 64


@pytest.fixture
def app(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'manifest.sqlite3'}",
        supervisor=object(),
        secure_cookies=False,
    )
    with app.state.runtime["session_factory"].begin() as db:
        db.add_all([
            UserRecord(id="owner", display_name="owner", can_create_project=True),
            UserRecord(id="outsider", display_name="outsider", can_create_project=True),
        ])
        db.flush()
        db.add_all([
            ProjectRecord(id="project", name="project", created_by="owner"),
            ProjectRecord(id="other-project", name="other", created_by="owner"),
        ])
        db.flush()
        db.add(MembershipRecord(project_id="project", user_id="owner", role="owner"))
        db.flush()
        paper = PaperRecord(id="paper", project_id="project", sha256=H, artifact_key="paper", bytes=1, page_count=1)
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(id="extraction", paper_id="paper", content_sha256=H, payload_json={})
        db.add(extraction)
        db.flush()
        experiment = ExperimentRecord(id="experiment", project_id="project", paper_id="paper", extraction_id="extraction", candidate_experiment_id="candidate", recipe_id="recipe", recipe_version="1")
        db.add(experiment)
        db.flush()
        revision = SpecRevisionRecord(id="revision", experiment_id="experiment", revision_no=1, spec_hash=H, policy_hash=H, spec_json={})
        db.add(revision)
        db.flush()
        db.add(RunRecord(id="run", project_id="project", experiment_id="experiment", revision_id="revision", manifest_hash=H, metrics_hash=H, report_hash=H, status="completed"))
        db.flush()
        db.add(AttemptRecord(id="attempt", run_id="run", attempt_index=0, source="template", status="validated", base_code_hash=H, validation_json={"secret": True}, code_manifest_json={"secret": True}, error_json={"secret": True}))
        db.flush()
        db.add(Job(id="job", kind="run_fixture", payload_ref="run", status="succeeded", fencing_token=1))
        db.flush()
        db.add(ExecutionRecord(id="execution", run_id="run", attempt_id="attempt", job_id="job", status="succeeded", runner_id="secret-runner", fencing_token=7, handle="handle", error_json={"code": "E_RUNNER", "message": "safe", "runner_id": "nested-runner", "fencing_token": 99, "storage_key": "/nested-secret", "secret": {"token": "hidden"}}))
        db.add(ExportRecord(id="export", run_id="run", status="succeeded", requested_formats_json=["report_json"], items_json=[{"format": "report_json", "status": "succeeded", "artifact_id": H, "storage_key": "/secret", "verification": {"secret": True}}], artifact_id=H, storage_key="/secret", sha256=H, bytes=12, media_type="application/json"))
        db.add(ExportRecord(id="invalid-export", run_id="run", status="succeeded", requested_formats_json=["report_json"], items_json=[{"format": "report_json", "status": "succeeded", "artifact_id": "b" * 64}], artifact_id="b" * 64, storage_key="/invalid", sha256="bad", bytes=13, media_type="application/json"))

    return app


def headers(actor="owner"):
    return {"X-Actor-ID": actor}


def test_manifest_is_enveloped_sorted_and_whitelisted(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/manifest", headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {"run_id", "status", "manifest_hash", "metrics_hash", "report_hash", "attempts", "executions", "exports"}
    assert data["run_id"] == "run"
    assert data["status"] == "provisional"
    assert data["attempts"][0]["attempt_id"] == "attempt"
    assert set(data["executions"][0]) == {"execution_id", "run_id", "attempt_id", "job_id", "status", "handle", "started_at", "finished_at", "error", "created_at"}
    assert "runner_id" not in response.text
    assert "fencing_token" not in response.text
    assert "lease_until" not in response.text
    assert "storage_key" not in response.text
    assert "verification" not in response.text
    assert set(data["exports"][0]) == {"id", "run_id", "status", "items", "artifact_id", "bytes", "created_at"}
    assert data["executions"][0]["error"] == {"code": "E_RUNNER", "message": "safe"}
    assert data["exports"][0]["items"] == [{"format": "report_json", "status": "succeeded", "artifact_id": H}]
    invalid = data["exports"][1]
    assert invalid["artifact_id"] is None
    assert invalid["bytes"] is None
    assert invalid["items"][0]["artifact_id"] is None


def test_manifest_returns_none_for_malformed_run_hashes(app):
    with app.state.runtime["session_factory"].begin() as db:
        run = db.get(RunRecord, "run")
        run.manifest_hash = "bad"
        run.metrics_hash = "g" * 64
        run.report_hash = None
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/manifest", headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["manifest_hash"] is None
    assert data["metrics_hash"] is None
    assert data["report_hash"] is None


def test_manifest_cross_project_is_resource_not_found(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/manifest", headers=headers("outsider"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
