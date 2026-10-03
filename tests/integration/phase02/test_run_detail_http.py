import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))

from p2r_api.main import create_app
from p2r_persistence.models import (
    ExperimentRecord,
    ExtractionRecord,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    RunEventRecord,
    RunRecord,
    SpecRevisionRecord,
    UserRecord,
)

H = "a" * 64


@pytest.fixture
def app(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'run-detail.sqlite3'}",
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
        db.add_all([
            PaperRecord(id="paper", project_id="project", sha256=H, artifact_key="paper", bytes=1, page_count=1),
            PaperRecord(id="other-paper", project_id="other-project", sha256=H, artifact_key="other-paper", bytes=1, page_count=1),
        ])
        db.flush()
        db.add_all([
            ExtractionRecord(id="extraction", paper_id="paper", content_sha256=H, payload_json={}),
            ExtractionRecord(id="other-extraction", paper_id="other-paper", content_sha256=H, payload_json={}),
        ])
        db.flush()
        db.add_all([
            ExperimentRecord(id="experiment", project_id="project", paper_id="paper", extraction_id="extraction",
                             candidate_experiment_id="candidate", recipe_id="recipe", recipe_version="1"),
            ExperimentRecord(id="other-experiment", project_id="other-project", paper_id="other-paper", extraction_id="other-extraction",
                             candidate_experiment_id="candidate", recipe_id="recipe", recipe_version="1"),
        ])
        db.flush()
        db.add_all([
            SpecRevisionRecord(id="revision", experiment_id="experiment", revision_no=1, spec_hash=H, policy_hash=H, spec_json={}),
            SpecRevisionRecord(id="other-revision", experiment_id="other-experiment", revision_no=1, spec_hash=H, policy_hash=H, spec_json={}),
        ])
        db.flush()
        db.add_all([
            RunRecord(id="run", project_id="project", experiment_id="experiment", revision_id="revision",
                      manifest_hash=H, status="failed", error_json={"code": "PIPELINE_FAILED", "message": "safe", "secret": "hidden"}),
            RunRecord(id="other-run", project_id="other-project", experiment_id="other-experiment", revision_id="other-revision",
                      manifest_hash=H, status="failed"),
        ])
        db.flush()
        db.add_all([
            RunEventRecord(run_id="run", seq=0, type="run.created", payload_json={}),
            RunEventRecord(run_id="run", seq=4, type="run.failed", payload_json={"secret": "hidden"}),
        ])
    return app


def test_run_detail_is_enveloped_whitelisted_and_uses_latest_event_seq(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run", headers={"X-Actor-ID": "owner"})

    assert response.status_code == 200, response.text
    body = response.json()
    data = body["data"]
    assert body["meta"]["request_id"]
    assert set(data) == {
        "run_id", "id", "project_id", "experiment_id", "revision_id", "spec_revision_id",
        "status", "mode", "manifest_hash", "metrics_hash", "report_hash", "verdict", "error",
        "last_event_seq", "created_at", "updated_at",
    }
    assert data["run_id"] == data["id"] == "run"
    assert data["last_event_seq"] == 4
    assert data["error"] == {"code": "PIPELINE_FAILED", "message": "safe"}
    assert "secret" not in response.text
    assert "runner_id" not in response.text
    assert "fencing_token" not in response.text
    assert "storage_key" not in response.text


def test_run_detail_cross_project_is_resource_not_found(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/other-run", headers={"X-Actor-ID": "outsider"})

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
