import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))

from p2r_api.main import create_app
from p2r_persistence.models import (
    AttemptRecord,
    ExperimentRecord,
    ExtractionRecord,
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
        db_url=f"sqlite:///{tmp_path / 'curves.sqlite3'}",
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
            ExperimentRecord(id="experiment", project_id="project", paper_id="paper", extraction_id="extraction", candidate_experiment_id="candidate", recipe_id="recipe", recipe_version="1"),
            ExperimentRecord(id="other-experiment", project_id="other-project", paper_id="other-paper", extraction_id="other-extraction", candidate_experiment_id="candidate", recipe_id="recipe", recipe_version="1"),
        ])
        db.flush()
        db.add_all([
            SpecRevisionRecord(id="revision", experiment_id="experiment", revision_no=1, spec_hash=H, policy_hash=H, spec_json={}),
            SpecRevisionRecord(id="other-revision", experiment_id="other-experiment", revision_no=1, spec_hash=H, policy_hash=H, spec_json={}),
        ])
        db.flush()
        db.add_all([
            RunRecord(id="run", project_id="project", experiment_id="experiment", revision_id="revision", manifest_hash=H, status="failed"),
            RunRecord(id="other-run", project_id="other-project", experiment_id="other-experiment", revision_id="other-revision", manifest_hash=H, status="failed"),
        ])
        db.flush()
        db.add_all([
            AttemptRecord(id="att-base", run_id="run", attempt_index=0, source="template", status="rejected", base_code_hash=H, validation_json={}, code_manifest_json={}),
            AttemptRecord(id="att-late", run_id="run", attempt_index=2, repair_ordinal=1, source="repair", status="validated", base_code_hash=H, patch_hash=H, validation_json={}, code_manifest_json={}),
            AttemptRecord(id="att-other", run_id="other-run", attempt_index=0, source="template", status="candidate", base_code_hash=H, validation_json={}, code_manifest_json={}),
        ])
    return app


def headers(actor="owner"):
    return {"X-Actor-ID": actor}


def test_curves_empty_contract_and_recent_attempt(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/curves", headers=headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["request_id"]
    assert set(body["data"]) == {"run_id", "attempt_id", "series", "downsampled", "warnings"}
    assert body["data"]["run_id"] == "run"
    assert body["data"]["attempt_id"] == "att-late"
    assert body["data"]["series"] == []
    assert body["data"]["downsampled"] is False
    assert body["data"]["warnings"]


def test_curves_same_run_attempt_is_selected_without_internal_fields(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/curves?attempt_id=att-base", headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["attempt_id"] == "att-base"
    assert "validation_json" not in response.text
    assert "code_manifest_json" not in response.text


def test_curves_cross_project_run_is_resource_not_found(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/other-run/curves", headers=headers("outsider"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("query", ["max_points=0", "max_points=2001", "max_points=not-an-int", "unexpected=true"])
def test_curves_rejects_invalid_query_parameters(app, query):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(f"/api/v1/runs/run/curves?{query}", headers=headers())
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_curves_rejects_attempt_from_another_run(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/curves?attempt_id=att-other", headers=headers())
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
