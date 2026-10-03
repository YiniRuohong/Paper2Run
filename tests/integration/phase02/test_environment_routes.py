from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from p2r_api.main import create_app
from p2r_persistence.models import (
    EnvironmentCheckRecord,
    ExperimentRecord,
    ExtractionRecord,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    SpecRevisionRecord,
    UserRecord,
)


@pytest.fixture
def seeded(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'environment.sqlite3'}", supervisor=object(), secure_cookies=False)
    with app.state.runtime["session_factory"].begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        project = ProjectRecord(name="environment", created_by=user.id)
        db.add(user)
        db.flush()
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        paper = PaperRecord(project_id=project.id, status="ready", sha256="a" * 64,
                            artifact_key="paper.pdf", bytes=1, page_count=1)
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(paper_id=paper.id, content_sha256="b" * 64,
                                      payload_json={"candidate_experiments": []})
        db.add(extraction)
        db.flush()
        experiment = ExperimentRecord(project_id=project.id, paper_id=paper.id,
                                      extraction_id=extraction.id,
                                      candidate_experiment_id="candidate",
                                      recipe_id="fixture", recipe_version="1")
        db.add(experiment)
        db.flush()
        revision = SpecRevisionRecord(experiment_id=experiment.id, revision_no=1,
                                     spec_hash="c" * 64, policy_hash="d" * 64,
                                     spec_json={"recipe": {"id": "fixture", "version": "1"}},
                                     validation_json={}, comparison_json={}, budget_json={})
        db.add(revision)
        db.flush()
        ids = {"project": project.id, "experiment": experiment.id, "revision": revision.id}
    return app, ids


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def test_environment_check_persists_safe_snapshot_and_replays(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        payload = {"revision_id": ids["revision"], "requirements": {"cpu_count": 1}}
        first = client.post(
            f"/api/v1/projects/{ids['project']}/experiments/{ids['experiment']}/environment-checks",
            headers=headers("env-1"), json=payload,
        )
        assert first.status_code == 201, first.text
        data = first.json()["data"]
        assert data["decision"] in {"ready", "blocked"}
        assert data["checker_version"] == "environment-checker.v1"
        assert all(set(item) >= {"key", "status", "required", "observed", "required_value", "suggestion"}
                   for item in data["items"])
        replay = client.post(
            f"/api/v1/projects/{ids['project']}/experiments/{ids['experiment']}/environment-checks",
            headers=headers("env-1"), json=payload,
        )
        assert replay.status_code == 201
        assert replay.json()["data"] == data
        listed = client.get(
            f"/api/v1/projects/{ids['project']}/experiments/{ids['experiment']}/environment-checks",
            headers={"X-Actor-ID": "owner"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["data"]) == 1
        fetched = client.get(
            f"/api/v1/environment-checks/{data['environment_check_id']}",
            headers={"X-Actor-ID": "owner"},
        )
        assert fetched.status_code == 200
        assert fetched.json()["data"] == data
        with app.state.runtime["session_factory"]() as db:
            assert db.query(EnvironmentCheckRecord).count() == 1


def test_environment_check_rejects_cross_project_revision(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/projects/{ids['project']}/experiments/{ids['experiment']}/environment-checks",
            headers=headers("env-cross"),
            json={"revision_id": "missing", "requirements": {}},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
