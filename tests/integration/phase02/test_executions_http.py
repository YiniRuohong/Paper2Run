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
        db_url=f"sqlite:///{tmp_path / 'executions.sqlite3'}",
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
        run = RunRecord(id="run", project_id="project", experiment_id="experiment", revision_id="revision", manifest_hash=H, status="running")
        db.add(run)
        db.flush()
        attempt = AttemptRecord(id="attempt", run_id="run", attempt_index=0, source="template", status="candidate", base_code_hash=H, validation_json={}, code_manifest_json={})
        db.add(attempt)
        db.flush()
        job = Job(id="job", kind="run_fixture", payload_ref="run", status="running", fencing_token=7, leased_by="runner-1")
        db.add(job)
        db.flush()
        db.add(ExecutionRecord(id="execution", run_id="run", attempt_id="attempt", job_id="job", status="running", runner_id="runner-1", fencing_token=7, handle="handle"))
    return app


def headers(actor="owner"):
    return {"X-Actor-ID": actor}


def test_heartbeat_success_updates_lease_without_secrets(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/heartbeat",
            headers=headers(),
            json={"runner_id": "runner-1", "fencing_token": 7, "lease_seconds": 30},
        )
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]) == {"execution_id", "status", "lease_seconds"}
    assert response.json()["data"] == {"execution_id": "execution", "status": "running", "lease_seconds": 30}
    assert "runner_id" not in response.text
    assert "fencing_token" not in response.text


def test_finish_success_whitelists_response_and_error(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/finish",
            headers=headers(),
            json={"runner_id": "runner-1", "fencing_token": 7, "status": "failed", "error": {"code": "FAILED"}},
        )
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]) == {"execution_id", "status", "error"}
    assert response.json()["data"] == {"execution_id": "execution", "status": "failed", "error": {"code": "FAILED"}}
    assert "runner_id" not in response.text
    assert "fencing_token" not in response.text


def test_finish_rejects_non_running_execution(app):
    with app.state.runtime["session_factory"].begin() as db:
        db.get(ExecutionRecord, "execution").status = "succeeded"
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/finish",
            headers=headers(),
            json={"runner_id": "runner-1", "fencing_token": 7, "status": "succeeded"},
        )
    assert response.status_code == 409, response.text


def test_execution_not_found_is_resource_error(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/missing/heartbeat",
            headers=headers(),
            json={"runner_id": "runner-1", "fencing_token": 7, "lease_seconds": 30},
        )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_stale_owner_or_fence_returns_409(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/heartbeat",
            headers=headers(),
            json={"runner_id": "stale-runner", "fencing_token": 7, "lease_seconds": 30},
        )
    assert response.status_code == 409, response.text


def test_execution_cross_project_without_membership_is_404(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/heartbeat",
            headers=headers("outsider"),
            json={"runner_id": "runner-1", "fencing_token": 7, "lease_seconds": 30},
        )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_execution_request_extra_fields_are_rejected(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/executions/execution/finish",
            headers=headers(),
            json={"runner_id": "runner-1", "fencing_token": 7, "status": "succeeded", "secret": "do-not-accept"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_execution_listing_is_project_scoped_and_whitelisted(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/executions", headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["execution_id"] == "execution"
    assert "fencing_token" not in data[0]


def test_execution_listing_hides_cross_project_resource(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/executions", headers=headers("outsider"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
