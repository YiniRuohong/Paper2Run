import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app
from p2r_persistence.models import (
    ExtractionRecord,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    UserRecord,
    SpecRevisionRecord,
)


@pytest.fixture
def seeded(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'protocol.sqlite3'}",
        # The supervisor argument opts into the existing development actor-header
        # harness; write routes then skip production session/CSRF checks.
        supervisor=object(),
        secure_cookies=False,
    )
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="protocol owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="Protocol project", created_by=user.id)
        other = ProjectRecord(name="Other project", created_by=user.id)
        db.add_all([project, other])
        db.flush()
        db.add_all([
            MembershipRecord(project_id=project.id, user_id=user.id, role="owner"),
            MembershipRecord(project_id=other.id, user_id=user.id, role="owner"),
        ])
        paper = PaperRecord(
            project_id=project.id, title="Fixture paper", status="ready",
            sha256="a" * 64, artifact_key="paper.pdf", bytes=1, page_count=1,
        )
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(
            paper_id=paper.id, content_sha256="b" * 64,
            payload_json={"candidate_experiments": [{
                "id": "candidate-1", "recipe_id": "offline_fixture", "recipe_version": "1",
                "label": "fixture", "execution_support": "supported",
            }]},
        )
        db.add(extraction)
        db.flush()
        ids = {"user": user.id, "project": project.id, "other": other.id,
               "paper": paper.id, "extraction": extraction.id}
    return app, ids


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def experiment_payload(ids):
    return {
        "paper_id": ids["paper"], "extraction_id": ids["extraction"],
        "candidate_experiment_id": "candidate-1", "recipe_id": "offline_fixture",
        "recipe_version": "1",
    }


def make_experiment(client, ids, key="experiment-1"):
    response = client.post(
        f"/api/v1/projects/{ids['project']}/experiments",
        headers=headers(key), json=experiment_payload(ids),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_revision(client, ids, experiment_id, key="revision-1", spec=None):
    payload = {"experiment_id": experiment_id, "base_revision_id": None,
               "spec": spec or {"schema_version": "1.0", "target": "fixture"}}
    response = client.post(
        f"/api/v1/projects/{ids['project']}/spec-revisions",
        headers=headers(key), json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_experiment_create_replay_conflict_and_cross_project_404(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        first = client.post(
            f"/api/v1/projects/{ids['project']}/experiments",
            headers=headers("exp-key"), json=experiment_payload(ids),
        )
        replay = client.post(
            f"/api/v1/projects/{ids['project']}/experiments",
            headers=headers("exp-key"), json=experiment_payload(ids),
        )
        changed = {**experiment_payload(ids), "recipe_version": "2"}
        conflict = client.post(
            f"/api/v1/projects/{ids['project']}/experiments",
            headers=headers("exp-key"), json=changed,
        )
        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["data"] == first.json()["data"]
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        missing = client.post(
            f"/api/v1/projects/{ids['other']}/experiments",
            headers=headers("other-key"), json=experiment_payload(ids),
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_revision_replay_conflict_and_confirmation_replay_hash_policy(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        exp = make_experiment(client, ids)
        revision = make_revision(client, ids, exp["experiment_id"])
        revision_payload = {"experiment_id": exp["experiment_id"], "base_revision_id": None,
                           "spec": {"schema_version": "1.0", "target": "fixture"}}
        replay = client.post(
            f"/api/v1/projects/{ids['project']}/spec-revisions",
            headers=headers("revision-1"), json=revision_payload,
        )
        conflict = client.post(
            f"/api/v1/projects/{ids['project']}/spec-revisions",
            headers=headers("revision-1"), json={**revision_payload, "spec": {"target": "changed"}},
        )
        assert replay.status_code == 201
        assert replay.json()["data"] == revision
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        confirmation_payload = {
            "spec_hash": revision["spec_hash"], "policy_hash": revision["policy_hash"],
            "accepted_issue_codes": [], "accept_budget": True,
        }
        first = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("confirm-1"), json=confirmation_payload,
        )
        replay = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("confirm-1"), json=confirmation_payload,
        )
        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["data"] == first.json()["data"]

        bad_hash = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("confirm-hash"),
            json={**confirmation_payload, "spec_hash": "0" * 64},
        )
        bad_policy = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("confirm-policy"),
            json={**confirmation_payload, "policy_hash": "0" * 64},
        )
        assert bad_hash.status_code == 409
        assert bad_hash.json()["error"]["code"] == "HASH_MISMATCH"
        assert bad_policy.status_code == 409
        assert bad_policy.json()["error"]["code"] == "POLICY_MISMATCH"


def test_confirmation_blocks_revision_with_blocking_issue(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        exp = make_experiment(client, ids)
        revision = make_revision(client, ids, exp["experiment_id"])
        factory = app.state.runtime["session_factory"]
        with factory.begin() as db:
            row = db.get(SpecRevisionRecord, revision["id"])
            row.validation_json = {"issues": [{"code": "BLOCK", "severity": "blocking"}]}
        response = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("confirm-block"),
            json={"spec_hash": revision["spec_hash"], "policy_hash": revision["policy_hash"],
                  "accepted_issue_codes": [], "accept_budget": True},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "BLOCKING_ISSUES"


def test_run_root_queued_job_idempotency_and_events_after(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        exp = make_experiment(client, ids)
        revision = make_revision(client, ids, exp["experiment_id"])
        confirmed = client.post(
            f"/api/v1/spec-revisions/{revision['id']}/confirm",
            headers=headers("run-confirm"),
            json={"spec_hash": revision["spec_hash"], "policy_hash": revision["policy_hash"],
                  "accepted_issue_codes": [], "accept_budget": True},
        ).json()["data"]
        with app.state.runtime["session_factory"].begin() as db:
            from p2r_persistence.models import EnvironmentCheckRecord, ExperimentRecord
            exp_row = db.get(ExperimentRecord, exp["experiment_id"])
            db.add(EnvironmentCheckRecord(
                project_id=ids["project"], experiment_id=exp["experiment_id"],
                revision_id=revision["id"], decision="ready", checker_version="test",
                requirements_json={}, items_json=[], suggestions_json=[],
            ))
        payload = {"revision_id": revision["id"], "confirmation_id": confirmed["confirmation_id"],
                   "mode": "local-cpu"}
        first = client.post(f"/api/v1/projects/{ids['project']}/runs",
                            headers=headers("run-key"), json=payload)
        replay = client.post(f"/api/v1/projects/{ids['project']}/runs",
                             headers=headers("run-key"), json=payload)
        assert first.status_code == 202
        assert first.json()["data"]["status"] == "queued"
        assert replay.status_code == 202
        assert replay.json()["data"] == first.json()["data"]
        run_id = first.json()["data"]["run_id"]
        with app.state.runtime["session_factory"]() as db:
            from p2r_persistence.models import Job, RunEventRecord
            assert db.scalar(select(Job).where(Job.payload_ref == run_id)) is not None
            assert db.scalar(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.seq == 0)).type == "run.created"
        events = client.get(f"/api/v1/runs/{run_id}/events", headers={"X-Actor-ID": "owner"})
        assert events.status_code == 200
        # The endpoint's default cursor is 0, so seq=0 is returned when
        # explicitly resuming from before the first event only in the DB-level
        # assertion above; after=0 is the empty post-cursor response.
        assert events.text == ""
        assert client.get(f"/api/v1/runs/{run_id}/events?after=0", headers={"X-Actor-ID": "owner"}).text == ""
