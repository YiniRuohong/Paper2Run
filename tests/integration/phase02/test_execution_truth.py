"""Task 6 execution-truth boundaries.

Run creation, the runner registry, and the report snapshot must only ever
describe what actually happened: a supported registered recipe, an explicitly
confirmed execution target, a gated environment, and the controlled local
smoke fixture that is never presented as a real paper reproduction.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app, create_demo_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_runner import (
    FixtureRunnerError,
    is_registered_recipe,
    reject_command,
    run_fixture,
)
from p2r_persistence.models import (
    ConfirmationRecord,
    EnvironmentCheckRecord,
    EvaluationRecord,
    ExperimentRecord,
    ExtractionRecord,
    Job,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    ReportBuildRecord,
    RunRecord,
    SpecRevisionRecord,
    SSHVerificationRecord,
    UserRecord,
)

PAPER_BYTES = b"Controlled smoke teaching fixture. Not a research paper, not a full reproduction."


@pytest.fixture
def seeded(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    address = store.put_bytes(PAPER_BYTES)
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'truth.sqlite3'}",
        supervisor=object(), secure_cookies=False, artifact_store=store,
    )
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="truth project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        paper = PaperRecord(
            project_id=project.id, title="fixture paper", status="ready",
            sha256=address.sha256, artifact_key=address.sha256,
            bytes=address.bytes, page_count=0,
            source_kind="text_fixture", media_type="text/plain",
        )
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(
            paper_id=paper.id, content_sha256=address.sha256,
            payload_json={"candidate_experiments": [
                {"id": "candidate-1", "recipe_id": "offline_fixture", "recipe_version": "1",
                 "label": "controlled smoke", "execution_support": "supported"},
                {"id": "candidate-bad", "recipe_id": "offline_fixture", "recipe_version": "1",
                 "label": "unsupported candidate", "execution_support": "unsupported"},
                {"id": "candidate-alt", "recipe_id": "other_recipe", "recipe_version": "1",
                 "label": "unregistered recipe", "execution_support": "supported"},
            ]},
        )
        db.add(extraction)
        db.flush()
        ids = {"project": project.id, "paper": paper.id, "extraction": extraction.id}
    return app, ids, store


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def make_confirmed_revision(client, ids, *, recipe_id="offline_fixture", recipe_version="1",
                            candidate_id="candidate-1", key="exp", actor="owner"):
    def hdr(suffix):
        base = {"Idempotency-Key": f"{key}-{suffix}"}
        return base | ({"X-Actor-ID": actor} if actor else {})

    experiment = client.post(
        f"/api/v1/projects/{ids['project']}/experiments",
        headers=hdr("experiment"),
        json={"paper_id": ids["paper"], "extraction_id": ids["extraction"],
              "candidate_experiment_id": candidate_id,
              "recipe_id": recipe_id, "recipe_version": recipe_version},
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["experiment_id"]
    revision = client.post(
        f"/api/v1/projects/{ids['project']}/spec-revisions",
        headers=hdr("revision"),
        json={"experiment_id": experiment_id, "base_revision_id": None,
              "spec": {"schema_version": "1.0", "target": "fixture"}},
    )
    assert revision.status_code == 201, revision.text
    revision = revision.json()["data"]
    confirmation = client.post(
        f"/api/v1/spec-revisions/{revision['id']}/confirm",
        headers=hdr("confirm"),
        json={"spec_hash": revision["spec_hash"], "policy_hash": revision["policy_hash"],
              "accepted_issue_codes": [], "accept_budget": True},
    )
    assert confirmation.status_code == 201, confirmation.text
    confirmation_id = confirmation.json()["data"]["confirmation_id"]
    return experiment_id, revision, confirmation_id


def add_environment_check(app, ids, experiment_id, revision_id, decision):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        row = EnvironmentCheckRecord(
            project_id=ids["project"], experiment_id=experiment_id,
            revision_id=revision_id, decision=decision, checker_version="test",
            requirements_json={}, items_json=[], suggestions_json=[],
        )
        db.add(row)
        db.flush()
        return row.id


def seed_unsupported_experiment(app, ids):
    """Seed an experiment whose candidate is explicitly unsupported.

    The confirm endpoint already blocks such revisions upstream, so the row
    set is created directly to prove the Run route enforces the same boundary
    on its own.
    """
    import hashlib
    import json

    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        experiment = ExperimentRecord(
            project_id=ids["project"], paper_id=ids["paper"],
            extraction_id=ids["extraction"], candidate_experiment_id="candidate-bad",
            recipe_id="offline_fixture", recipe_version="1")
        db.add(experiment)
        db.flush()
        spec = {"schema_version": "1.0", "target": "fixture"}
        spec_hash = hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        revision = SpecRevisionRecord(
            experiment_id=experiment.id, revision_no=1,
            spec_hash=spec_hash, policy_hash="d" * 64, spec_json=spec)
        db.add(revision)
        db.flush()
        confirmation = ConfirmationRecord(
            revision_id=revision.id, actor_id="owner",
            spec_hash=spec_hash, policy_hash="d" * 64)
        db.add(confirmation)
        db.flush()
        return experiment.id, revision.id, confirmation.id


def add_ssh_verification(app, ids, experiment_id, revision_id, check_id, status="awaiting_confirmation"):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        row = SSHVerificationRecord(
            environment_check_id=check_id, project_id=ids["project"],
            experiment_id=experiment_id, revision_id=revision_id, status=status,
            target_json={"host": "10.0.0.5", "port": 22, "user": "runner", "auth_type": "agent"},
            summary_json={"connected": True},
        )
        db.add(row)
        db.flush()
        return row.id


def create_run(client, ids, revision_id, confirmation_id, *, mode="local-cpu", execution=None, key="run"):
    payload = {"revision_id": revision_id, "confirmation_id": confirmation_id, "mode": mode}
    if execution is not None:
        payload["execution"] = execution
    return client.post(
        f"/api/v1/projects/{ids['project']}/runs", headers=headers(key), json=payload,
    )


def test_run_requires_registered_recipe_and_supported_candidate(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        # A recipe outside the controlled runner registry is rejected.
        experiment_id, revision, confirmation = make_confirmed_revision(
            client, ids, candidate_id="candidate-alt",
            recipe_id="other_recipe", recipe_version="1", key="unregistered")
        add_environment_check(app, ids, experiment_id, revision["id"], "ready")
        response = create_run(client, ids, revision["id"], confirmation, key="unregistered-run")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "UNSUPPORTED_RECIPE"
        # A candidate that is not explicitly supported is rejected. The row set
        # is seeded directly because the confirm endpoint already blocks this
        # configuration upstream (BLOCKING_ISSUES).
        experiment_id, revision_id, confirmation_id = seed_unsupported_experiment(app, ids)
        add_environment_check(app, ids, experiment_id, revision_id, "ready")
        response = create_run(client, ids, revision_id, confirmation_id, key="unsupported-run")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "UNSUPPORTED_RECIPE"
        # The supported, registered combination is accepted.
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="ok")
        add_environment_check(app, ids, experiment_id, revision["id"], "ready")
        response = create_run(client, ids, revision["id"], confirmation, key="ok-run")
        assert response.status_code == 202, response.text
        assert response.json()["data"]["status"] == "queued"


def test_run_requires_ready_environment(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="env")
        # No environment check at all: the harness must prepare one explicitly.
        response = create_run(client, ids, revision["id"], confirmation, key="env-missing")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ENVIRONMENT_CHECK_REQUIRED"
        # A blocked environment check refuses execution.
        add_environment_check(app, ids, experiment_id, revision["id"], "blocked")
        response = create_run(client, ids, revision["id"], confirmation, key="env-blocked")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ENVIRONMENT_BLOCKED"
        with app.state.runtime["session_factory"]() as db:
            assert db.scalars(select(RunRecord)).all() == []


def test_unverified_ssh_or_unconfirmed_target_never_creates_a_run(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="ssh")
        check_id = add_environment_check(app, ids, experiment_id, revision["id"], "blocked")
        verification_id = add_ssh_verification(app, ids, experiment_id, revision["id"], check_id)
        runs_url = f"/api/v1/projects/{ids['project']}/runs"
        # The verification alone never created a Run.
        assert client.get(runs_url, headers={"X-Actor-ID": "owner"}).json()["data"] == []
        # ssh mode without a verification binding is a validation error.
        response = create_run(client, ids, revision["id"], confirmation, mode="ssh", key="ssh-none")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        # A binding without the explicit execution confirmation is refused.
        response = create_run(
            client, ids, revision["id"], confirmation, mode="ssh", key="ssh-unconfirmed",
            execution={"kind": "ssh", "ssh_verification_id": verification_id, "confirmed": False})
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "EXECUTION_TARGET_NOT_CONFIRMED"
        # An unknown verification id cannot be bound.
        response = create_run(
            client, ids, revision["id"], confirmation, mode="ssh", key="ssh-unknown",
            execution={"kind": "ssh", "ssh_verification_id": "sshv_missing", "confirmed": True})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        # A verification that is no longer awaiting confirmation cannot be bound.
        consumed_id = add_ssh_verification(app, ids, experiment_id, revision["id"], check_id, status="consumed")
        response = create_run(
            client, ids, revision["id"], confirmation, mode="ssh", key="ssh-consumed",
            execution={"kind": "ssh", "ssh_verification_id": consumed_id, "confirmed": True})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SSH_VERIFICATION_REQUIRED"
        with app.state.runtime["session_factory"]() as db:
            assert db.scalars(select(RunRecord)).all() == []
            assert db.scalar(select(Job)) is None


def test_confirmed_ssh_run_is_recorded_but_never_locally_executed(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="sshrun")
        check_id = add_environment_check(app, ids, experiment_id, revision["id"], "blocked")
        verification_id = add_ssh_verification(app, ids, experiment_id, revision["id"], check_id)
        response = create_run(
            client, ids, revision["id"], confirmation, mode="ssh", key="sshrun-run",
            execution={"kind": "ssh", "ssh_verification_id": verification_id, "confirmed": True})
        assert response.status_code == 202, response.text
        run_id = response.json()["data"]["run_id"]
        assert response.json()["data"]["status"] == "created"
        with app.state.runtime["session_factory"]() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "created"
            assert run.mode == "ssh"
            assert run.environment_check_id == check_id
            assert run.target_json["kind"] == "ssh"
            assert run.target_json["ssh_verification_id"] == verification_id
            assert run.target_json["target"]["host"] == "10.0.0.5"
            # Nothing is enqueued: no local fixture output may pose as a
            # remote execution while no controlled ssh adapter exists.
            assert db.scalar(select(Job).where(Job.payload_ref == run_id)) is None
            verification = db.get(SSHVerificationRecord, verification_id)
            assert verification.status == "awaiting_confirmation"
        # A local-cpu body carrying an ssh binding is rejected.
        experiment_id2, revision2, confirmation2 = make_confirmed_revision(client, ids, key="mix")
        response = create_run(
            client, ids, revision2["id"], confirmation2, mode="local-cpu", key="mix-run",
            execution={"kind": "ssh", "ssh_verification_id": verification_id, "confirmed": True})
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_report_requires_completed_run_and_evaluation(seeded):
    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="rep")
        add_environment_check(app, ids, experiment_id, revision["id"], "ready")
        created = create_run(client, ids, revision["id"], confirmation, key="rep-run").json()["data"]
        # A queued (never successfully executed) Run cannot produce a report.
        response = client.post(
            f"/api/v1/runs/{created['run_id']}/report-builds",
            headers=headers("rep-queued"), json={})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "RUN_NOT_COMPLETED"
        # A completed Run without a recorded evaluation is rejected as invalid.
        with factory.begin() as db:
            db.add(RunRecord(
                project_id=ids["project"], experiment_id=experiment_id,
                revision_id=revision["id"], status="completed", mode="local-cpu",
                manifest_hash="e" * 64,
                environment_check_id=add_environment_check(app, ids, experiment_id, revision["id"], "ready"),
                target_json={"kind": "local-cpu", "runner": "local-fixture-v1"},
            ))
        # The status must stay completed with no metrics to freeze.
        with factory() as db:
            orphan = db.scalars(select(RunRecord).where(RunRecord.status == "completed")).all()
            assert len(orphan) == 1
        response = client.post(
            f"/api/v1/runs/{orphan[0].id}/report-builds",
            headers=headers("rep-orphan"), json={})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "REPORT_INPUT_INVALID"


def test_report_snapshot_carries_execution_truth_and_controlled_smoke_markers(seeded):
    from p2r_api import demo as demo_runtime
    from p2r_persistence.models import EvaluationRecord, ReportRecord

    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation = make_confirmed_revision(client, ids, key="truth")
        check_id = add_environment_check(app, ids, experiment_id, revision["id"], "ready")
        created = create_run(client, ids, revision["id"], confirmation, key="truth-run").json()["data"]
        run_id = created["run_id"]
        # Run the controlled fixture for this Run through the guided demo path.
        assert demo_runtime.process_next_demo_job(factory, store, worker_id="guided-demo")
        with factory() as db:
            assert db.get(RunRecord, run_id).status == "completed"
        build = client.post(
            f"/api/v1/runs/{run_id}/report-builds", headers=headers("truth-report"), json={})
        assert build.status_code == 202, build.text
        build_id = build.json()["data"]["build_id"]
        with factory() as db:
            snapshot = db.get(ReportBuildRecord, build_id).input_json
        assert snapshot["target"] == {"kind": "local-cpu", "runner": "local-fixture-v1"}
        assert snapshot["environment"]["recorded"] is True
        assert snapshot["environment"]["decision"] == "ready"
        assert snapshot["environment"]["environment_check_id"] == check_id
        assert snapshot["recipe"] == {"id": "offline_fixture", "version": "1"}
        assert snapshot["source"]["sha256"] and snapshot["source"]["bytes"] > 0
        assert snapshot["limitations"] == ["local demo", "controlled smoke", "not full reproduction"]
        assert snapshot["controlled_smoke"] is True
        assert snapshot["evidence_class"] == "controlled_smoke"
        assert len(snapshot["evidence"]) == 1
        assert set(snapshot["evidence"][0]) == {"run_id", "sha256", "bytes", "media_type"}
        from p2r_orchestration.report_jobs import process_next_report_job
        assert process_next_report_job(factory)
        report = client.get(f"/api/v1/runs/{run_id}/report", headers={"X-Actor-ID": "owner"})
        assert report.status_code == 200, report.text
        content = report.json()["data"]
        assert content["limitations"] == snapshot["limitations"]
        assert content["evidence"] == snapshot["evidence"]
        with factory() as db:
            assert db.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)) is not None


def test_runner_registry_rejects_everything_unregistered():
    assert is_registered_recipe("offline_fixture", "1") is True
    assert is_registered_recipe("offline_fixture", "2") is False
    assert is_registered_recipe("other_recipe", "1") is False
    with pytest.raises(FixtureRunnerError):
        run_fixture(recipe_id="unknown_recipe", recipe_version="1")
    with pytest.raises(FixtureRunnerError):
        run_fixture(recipe_id="offline_fixture", recipe_version="9")
    with pytest.raises(FixtureRunnerError):
        run_fixture(recipe_id="offline_fixture", recipe_version="1", target="ssh")
    with pytest.raises(FixtureRunnerError):
        run_fixture(recipe_id="offline_fixture", recipe_version="1", parameters={"lr": 0.1})
    # The keyword-only signature cannot even express shell/argv/workdir.
    with pytest.raises(TypeError):
        run_fixture(recipe_id="offline_fixture", recipe_version="1", shell="/bin/sh")
    with pytest.raises(TypeError):
        run_fixture(recipe_id="offline_fixture", recipe_version="1", argv=["ls", "-la"])
    with pytest.raises(TypeError):
        run_fixture(recipe_id="offline_fixture", recipe_version="1", workdir="/tmp")
    with pytest.raises(FixtureRunnerError):
        reject_command("ls", "-la")
    result = run_fixture(recipe_id="offline_fixture", recipe_version="1")
    assert result["target"] == {"kind": "local-cpu", "runner": "local-fixture-v1"}
    assert result["evidence_class"] == "controlled_smoke"
    assert result["predictions"]


def test_guided_demo_controlled_smoke_chain_stays_compatible(tmp_path):
    """The demo fixture path keeps working end to end: the real checker runs
    once, records its decision, and passes the same ready gate; the finished
    report is marked as controlled smoke, never as a real reproduction."""
    from p2r_api import demo as demo_runtime
    import threading
    import time

    store = LocalArtifactStore(tmp_path / "artifacts")
    app = create_demo_app(db_url=f"sqlite:///{tmp_path / 'demo.sqlite3'}", artifact_store=store)
    factory = app.state.runtime["session_factory"]

    def wait_for(predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            threading.Event().wait(0.02)
        return predicate()

    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/demo/bootstrap", headers={"Idempotency-Key": "bootstrap"},
            json={"fixture_id": "offline-smoke-v1", "source_processing_consent": True})
        assert bootstrap.status_code == 201, bootstrap.text
        data = bootstrap.json()["data"]
        demo_ids = {"project": data["project_id"], "paper": data["paper_id"],
                    "extraction": data["extraction_id"]}
        experiment_id, revision, confirmation_id = make_confirmed_revision(
            client, demo_ids, candidate_id="offline-smoke", key="demo", actor=None)
        created = client.post(
            f"/api/v1/projects/{data['project_id']}/runs",
            headers={"Idempotency-Key": "demo-run"},
            json={"spec_revision_id": revision["id"], "confirmation_id": confirmation_id},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]
        assert created.json()["data"]["status"] == "queued"
        with factory() as db:
            checks = db.scalars(select(EnvironmentCheckRecord).where(
                EnvironmentCheckRecord.revision_id == revision["id"])).all()
            assert len(checks) == 1
            assert checks[0].decision == "ready"
            assert checks[0].checker_version != "test"
        assert wait_for(lambda: client.get(
            f"/api/v1/runs/{run_id}").json()["data"]["status"] == "completed")
        build = client.post(
            f"/api/v1/runs/{run_id}/report-builds",
            headers={"Idempotency-Key": "demo-report"}, json={})
        assert build.status_code == 202, build.text
        build_id = build.json()["data"]["build_id"]
        assert wait_for(lambda: client.get(
            f"/api/v1/runs/{run_id}/report-status").json()["data"]["status"] == "ready")
        with factory() as db:
            snapshot = db.get(ReportBuildRecord, build_id).input_json
        assert snapshot["environment"]["recorded"] is True
        assert snapshot["environment"]["decision"] == "ready"
        assert snapshot["target"] == {"kind": "local-cpu", "runner": "local-fixture-v1"}
        assert snapshot["controlled_smoke"] is True
        assert snapshot["evidence_class"] == "controlled_smoke"
