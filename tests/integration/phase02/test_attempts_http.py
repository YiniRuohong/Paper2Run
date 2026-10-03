import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
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
        db_url=f"sqlite:///{tmp_path / 'attempts.sqlite3'}",
        supervisor=object(),
        secure_cookies=False,
    )
    with app.state.runtime["session_factory"].begin() as db:
        owner = UserRecord(id="owner", display_name="owner", can_create_project=True)
        outsider = UserRecord(id="outsider", display_name="outsider", can_create_project=True)
        db.add_all([owner, outsider])
        db.flush()

        project = ProjectRecord(id="project", name="project", created_by="owner")
        other_project = ProjectRecord(id="other", name="other", created_by="owner")
        db.add_all([project, other_project])
        db.flush()
        db.add(MembershipRecord(project_id="project", user_id="owner", role="owner"))
        db.flush()

        paper = PaperRecord(id="paper", project_id="project", sha256=H, artifact_key="x", bytes=1, page_count=1)
        other_paper = PaperRecord(id="other-paper", project_id="other", sha256=H, artifact_key="y", bytes=1, page_count=1)
        db.add_all([paper, other_paper])
        db.flush()
        extraction = ExtractionRecord(id="ext", paper_id="paper", content_sha256=H, payload_json={})
        other_extraction = ExtractionRecord(id="other-ext", paper_id="other-paper", content_sha256=H, payload_json={})
        db.add_all([extraction, other_extraction])
        db.flush()
        experiment = ExperimentRecord(id="exp", project_id="project", paper_id="paper", extraction_id="ext", candidate_experiment_id="c", recipe_id="r", recipe_version="1")
        other_experiment = ExperimentRecord(id="other-exp", project_id="other", paper_id="other-paper", extraction_id="other-ext", candidate_experiment_id="c", recipe_id="r", recipe_version="1")
        db.add_all([experiment, other_experiment])
        db.flush()
        revision = SpecRevisionRecord(id="rev", experiment_id="exp", revision_no=1, spec_hash=H, policy_hash=H, spec_json={})
        other_revision = SpecRevisionRecord(id="other-rev", experiment_id="other-exp", revision_no=1, spec_hash=H, policy_hash=H, spec_json={})
        db.add_all([revision, other_revision])
        db.flush()
        run = RunRecord(id="run", project_id="project", experiment_id="exp", revision_id="rev", manifest_hash=H, status="failed")
        other_run = RunRecord(id="other-run", project_id="other", experiment_id="other-exp", revision_id="other-rev", manifest_hash=H, status="failed")
        db.add_all([run, other_run])
        db.flush()
        db.add_all([
            AttemptRecord(id="att-late", run_id="run", attempt_index=2, repair_ordinal=1, source="repair", status="validated", base_code_hash=H, patch_hash=H, validation_json={"secret": True}, code_manifest_json={"secret": True}, error_json={"secret": True}),
            AttemptRecord(id="att-base", run_id="run", attempt_index=0, source="template", status="rejected", base_code_hash=H, validation_json={}, code_manifest_json={}),
            AttemptRecord(id="att-other", run_id="other-run", attempt_index=0, source="template", status="candidate", base_code_hash=H, validation_json={}, code_manifest_json={}),
        ])
    return app


def headers(actor="owner"):
    return {"X-Actor-ID": actor}


def test_attempts_http_empty_list(app):
    with app.state.runtime["session_factory"].begin() as db:
        run = db.get(RunRecord, "run")
        db.query(AttemptRecord).filter(AttemptRecord.run_id == run.id).delete()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/attempts", headers=headers())
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


def test_attempts_http_sorted_and_whitelist(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/attempts", headers=headers())
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert [row["attempt_id"] for row in rows] == ["att-base", "att-late"]
    assert [row["attempt_index"] for row in rows] == [0, 2]
    assert set(rows[1]) == {
        "attempt_id", "run_id", "attempt_index", "repair_ordinal", "source", "status",
        "base_code_hash", "patch_hash", "created_at", "updated_at",
    }
    assert "validation_json" not in rows[1]
    assert "code_manifest_json" not in rows[1]
    assert "error_json" not in rows[1]


def test_attempts_http_does_not_mix_another_run_in_same_project(app):
    with app.state.runtime["session_factory"].begin() as db:
        run = db.get(RunRecord, "run")
        sibling = RunRecord(id="sibling-run", project_id=run.project_id, experiment_id=run.experiment_id, revision_id=run.revision_id, manifest_hash=H, status="failed")
        db.add(sibling)
        db.flush()
        db.add(AttemptRecord(id="att-same-project", run_id=sibling.id, attempt_index=3, source="model", status="candidate", base_code_hash=H, validation_json={}, code_manifest_json={}))
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/run/attempts", headers=headers())
    assert response.status_code == 200
    assert all(row["run_id"] == "run" for row in response.json()["data"])


def test_attempts_http_cross_project_without_membership_is_404(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/runs/other-run/attempts", headers=headers("outsider"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_manifest_artifact_files_and_text_are_verified(app, tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    app.state.runtime["artifact_store"] = store
    content = "你好\nworld\n".encode()
    address = store.put_bytes(content)
    with app.state.runtime["session_factory"].begin() as db:
        db.add(AttemptRecord(id="manifest-att", run_id="run", attempt_index=1, source="model", status="candidate", base_code_hash=H,
                             code_manifest_json={"files": [{"path": "out.txt", "artifact_id": address.sha256, "sha256": address.sha256, "bytes": address.bytes, "media_type": "text/plain"},
                                                  {"path": "bad-hash.txt", "artifact_id": address.sha256, "sha256": "0" * 64, "bytes": address.bytes, "media_type": "text/plain"},
                                                  {"path": "bad-bytes.txt", "artifact_id": address.sha256, "sha256": address.sha256, "bytes": address.bytes + 1, "media_type": "text/plain"},
                                                  {"path": "missing.txt", "artifact_id": "b" * 64, "sha256": "b" * 64, "bytes": 1, "media_type": "text/plain"}]}, validation_json={}))
    with TestClient(app, base_url="https://testserver") as client:
        files = client.get("/api/v1/attempts/manifest-att/files", headers=headers())
        assert files.status_code == 200
        assert [item["path"] for item in files.json()["data"]] == ["out.txt"]
        text = client.get(f"/api/v1/artifacts/{address.sha256}/text?offset=4&limit=6", headers=headers())
        assert text.status_code == 200, text.text
        assert text.json()["data"]["text"] == "world\n"
        assert client.get(f"/api/v1/artifacts/{address.sha256}/text?offset=1", headers=headers()).status_code == 422
        assert client.get(f"/api/v1/artifacts/{address.sha256}/text?offset=999", headers=headers()).status_code == 422


def test_manifest_files_and_text_reject_invalid_query_parameters(app):
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/api/v1/attempts/att-base/files?unexpected=true", "/api/v1/attempts/att-base/files?limit=nope"):
            response = client.get(path, headers=headers())
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        response = client.get("/api/v1/artifacts/" + "a" * 64 + "/text?offset=nope", headers=headers())
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
