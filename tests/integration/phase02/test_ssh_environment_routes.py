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
        outsider = UserRecord(id="outsider", display_name="outsider", can_create_project=True)
        project = ProjectRecord(name="environment", created_by=user.id)
        db.add_all([user, outsider])
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


SSH_TARGET = {
    "host": "10.0.0.5",
    "port": 22,
    "user": "runner",
    "workdir": "/srv/p2r",
    "auth": {"type": "agent", "agent_id": "agent-default"},
}


def create_check(app, ids, key, requirements=None):
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/projects/{ids['project']}/experiments/{ids['experiment']}/environment-checks",
            headers=headers(key),
            json={"revision_id": ids["revision"], "requirements": requirements or {}},
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]


def ssh_url(check_id):
    return f"/api/v1/environment-checks/{check_id}/ssh-verifications"


class FakeSSHVerifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def verify(self, target, recipe):
        from p2r_orchestration.ssh_executor import SSHVerificationResult, SSHVerificationStatus

        self.calls += 1
        return SSHVerificationResult(
            status=SSHVerificationStatus.VERIFIED if self.ok else SSHVerificationStatus.FAILED,
            summary={"connected": self.ok, "recipe_id": getattr(recipe, "recipe_id", None)},
        )


def test_ssh_verification_success_creates_verification_and_no_run(seeded):
    app, ids = seeded
    verifier = FakeSSHVerifier(ok=True)
    app.state.runtime["ssh_verifier"] = verifier
    check = create_check(app, ids, "env-a", {"cpu_count": 10_000})  # forced blocked decision
    assert check["decision"] == "blocked"
    with TestClient(app) as client:
        response = client.post(
            ssh_url(check["environment_check_id"]),
            headers=headers("ssh-1"),
            json={"target": SSH_TARGET, "user_selected": True},
        )
        assert response.status_code in (200, 201), response.text
        data = response.json()["data"]
        assert data["ssh_verification_id"]
        assert data["status"] == "awaiting_confirmation"
        assert data["environment_check_id"] == check["environment_check_id"]
        # safe target projection only — no credential material of any kind
        assert set(data["target"]) <= {"host", "port", "user", "workdir", "auth_type"}
        assert "password" not in str(data).lower()
        assert "BEGIN" not in str(data)
        # verification never creates a run
        runs = client.get(
            f"/api/v1/projects/{ids['project']}/runs",
            headers={"X-Actor-ID": "owner"},
        )
        assert runs.status_code == 200
        assert runs.json()["data"] == []
        with app.state.runtime["session_factory"]() as db:
            from p2r_persistence.models import SSHVerificationRecord, RunRecord

            assert db.query(SSHVerificationRecord).count() == 1
            assert db.query(RunRecord).count() == 0
        assert verifier.calls == 1


def test_ssh_verification_failed_creates_no_verification_record(seeded):
    app, ids = seeded
    verifier = FakeSSHVerifier(ok=False)
    app.state.runtime["ssh_verifier"] = verifier
    check = create_check(app, ids, "env-b", {"cpu_count": 10_000})
    with TestClient(app) as client:
        response = client.post(
            ssh_url(check["environment_check_id"]),
            headers=headers("ssh-2"),
            json={"target": SSH_TARGET, "user_selected": True},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SSH_VERIFICATION_FAILED"
        with app.state.runtime["session_factory"]() as db:
            from p2r_persistence.models import SSHVerificationRecord, RunRecord

            assert db.query(SSHVerificationRecord).count() == 0
            assert db.query(RunRecord).count() == 0


def test_ssh_verification_requires_blocked_check_and_user_selection(seeded):
    app, ids = seeded
    app.state.runtime["ssh_verifier"] = FakeSSHVerifier(ok=True)
    ready = create_check(app, ids, "env-c")  # no requirements -> ready
    assert ready["decision"] == "ready"
    with TestClient(app) as client:
        # ready check must not fall back to ssh
        response = client.post(
            ssh_url(ready["environment_check_id"]),
            headers=headers("ssh-3"),
            json={"target": SSH_TARGET, "user_selected": True},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SSH_NOT_AVAILABLE"
        blocked = create_check(app, ids, "env-d", {"cpu_count": 10_000})
        # explicit user selection is required
        response = client.post(
            ssh_url(blocked["environment_check_id"]),
            headers=headers("ssh-4"),
            json={"target": SSH_TARGET, "user_selected": False},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_ssh_verification_rejects_secret_and_arbitrary_command_fields(seeded):
    app, ids = seeded
    app.state.runtime["ssh_verifier"] = FakeSSHVerifier(ok=True)
    check = create_check(app, ids, "env-e", {"cpu_count": 10_000})
    with TestClient(app) as client:
        for i, bad in enumerate((
            {"target": {**SSH_TARGET, "auth": {"type": "password", "password": "x"}}, "user_selected": True},
            {"target": {**SSH_TARGET, "command": "rm -rf /"}, "user_selected": True},
            {"target": SSH_TARGET, "user_selected": True, "extra": 1},
        )):
            response = client.post(
                ssh_url(check["environment_check_id"]),
                headers=headers(f"ssh-bad-{i}"),
                json=bad,
            )
            assert response.status_code == 422, response.text
        with app.state.runtime["session_factory"]() as db:
            from p2r_persistence.models import SSHVerificationRecord

            assert db.query(SSHVerificationRecord).count() == 0


def test_ssh_verification_is_idempotent_and_readable(seeded):
    app, ids = seeded
    verifier = FakeSSHVerifier(ok=True)
    app.state.runtime["ssh_verifier"] = verifier
    check = create_check(app, ids, "env-f", {"cpu_count": 10_000})
    payload = {"target": SSH_TARGET, "user_selected": True}
    with TestClient(app) as client:
        first = client.post(ssh_url(check["environment_check_id"]), headers=headers("ssh-idem"), json=payload)
        assert first.status_code in (200, 201), first.text
        data = first.json()["data"]
        replay = client.post(ssh_url(check["environment_check_id"]), headers=headers("ssh-idem"), json=payload)
        assert replay.status_code == first.status_code
        assert replay.json()["data"] == data
        assert verifier.calls == 1  # replay does not re-invoke the verifier
        fetched = client.get(
            f"/api/v1/ssh-verifications/{data['ssh_verification_id']}",
            headers={"X-Actor-ID": "owner"},
        )
        assert fetched.status_code == 200
        assert fetched.json()["data"] == data


def test_ssh_verification_requires_idempotency_key_and_project_access(seeded):
    app, ids = seeded
    app.state.runtime["ssh_verifier"] = FakeSSHVerifier(ok=True)
    check = create_check(app, ids, "env-g", {"cpu_count": 10_000})
    with TestClient(app) as client:
        response = client.post(
            ssh_url(check["environment_check_id"]),
            headers={"X-Actor-ID": "owner"},
            json={"target": SSH_TARGET, "user_selected": True},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        # a non-member actor cannot read or write verifications
        outsider = client.post(
            ssh_url(check["environment_check_id"]),
            headers={"X-Actor-ID": "outsider", "Idempotency-Key": "ssh-out"},
            json={"target": SSH_TARGET, "user_selected": True},
        )
        assert outsider.status_code == 404
