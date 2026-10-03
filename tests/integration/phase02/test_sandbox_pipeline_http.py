"""Sandboxed model-code pipeline: codegen attempt -> sandbox Run -> report.

The sandbox_python/1 recipe executes model-generated, manifest-pinned code in
the host sandbox. The chain must stay honest end to end: run creation refuses
without a reviewed code attempt, the worker executes exactly the
content-addressed bytes, failures never produce a success report, and the
report carries the sandboxed_model_code evidence class with its own
limitations.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api import demo as demo_runtime
from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.evidence_registry import EVIDENCE_EVENT_TYPE
from p2r_orchestration.run_worker import CODE_BOUND_EVENT_TYPE
from p2r_orchestration.report_builder import SANDBOX_LIMITATIONS
from p2r_persistence.models import (
    AttemptRecord,
    EnvironmentCheckRecord,
    EvaluationRecord,
    ExecutionRecord,
    ExtractionRecord,
    MembershipRecord,
    PaperRecord,
    ProjectRecord,
    ReportBuildRecord,
    RunEventRecord,
    RunRecord,
    UserRecord,
)

PAPER_BYTES = b"Sandboxed self-contained reproduction article: a weighted k-NN baseline on a deterministic synthetic dataset."

GUEST_CODE = """\
import json, random
rng = random.Random(20260930)
rows = []
for _ in range(60):
    x = rng.uniform(-1, 1)
    y = rng.uniform(-1, 1)
    rows.append((x, y, 1 if x + y > 0 else 0))
test = rows[40:]
predictions = [{"id": "row-%03d" % i, "prediction": 1 if p[0] + p[1] > 0.05 else 0} for i, p in enumerate(test)]
labels = {"row-%03d" % i: p[2] for i, p in enumerate(test)}
with open("predictions.json", "w") as handle:
    json.dump(predictions, handle)
with open("labels.json", "w") as handle:
    json.dump(labels, handle)
print("guest complete")
"""

FAILING_CODE = "import sys\nprint('about to fail', file=sys.stderr)\nsys.exit(3)\n"
SLEEPING_CODE = "import time\ntime.sleep(60)\n"


@pytest.fixture
def seeded(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    address = store.put_bytes(PAPER_BYTES)
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'sandbox.sqlite3'}",
        supervisor=object(), secure_cookies=False, artifact_store=store,
    )
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="sandbox project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        paper = PaperRecord(
            project_id=project.id, title="knn sandbox paper", status="ready",
            sha256=address.sha256, artifact_key=address.sha256,
            bytes=address.bytes, page_count=0,
            source_kind="text_fixture", media_type="text/plain",
        )
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(
            paper_id=paper.id, content_sha256=address.sha256,
            payload_json={"candidate_experiments": [
                {"id": "candidate-sbx", "recipe_id": "sandbox_python", "recipe_version": "1",
                 "label": "self-contained knn reproduction", "execution_support": "supported"},
            ]},
        )
        db.add(extraction)
        db.flush()
        ids = {"project": project.id, "paper": paper.id, "extraction": extraction.id}
    return app, ids, store


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def make_confirmed_sandbox_revision(client, ids, *, training=None, key="sbx"):
    def hdr(suffix):
        return {"X-Actor-ID": "owner", "Idempotency-Key": f"{key}-{suffix}"}

    experiment = client.post(
        f"/api/v1/projects/{ids['project']}/experiments",
        headers=hdr("experiment"),
        json={"paper_id": ids["paper"], "extraction_id": ids["extraction"],
              "candidate_experiment_id": "candidate-sbx",
              "recipe_id": "sandbox_python", "recipe_version": "1"},
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["experiment_id"]
    revision = client.post(
        f"/api/v1/projects/{ids['project']}/spec-revisions",
        headers=hdr("revision"),
        json={"experiment_id": experiment_id, "base_revision_id": None,
              "spec": {"schema_version": "1.0", "target": "local-cpu",
                       "training": training if training is not None else {"seed": 7, "max_seconds": 30}}},
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
    return experiment_id, revision, confirmation.json()["data"]["confirmation_id"]


def add_model_attempt(app, store, experiment_id, code, *, attempt_index=1):
    address = store.put_bytes(code.encode("utf-8"))
    manifest = {
        "code_artifact_id": address.sha256,
        "code_sha256": address.sha256,
        "generator_model": "test-generator",
        "prompt_version": "codegen-prompt-1",
        "policy_version": "codegen-policy-1",
    }
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        db.add(AttemptRecord(
            experiment_id=experiment_id, run_id=None, source="model",
            status="candidate", attempt_index=attempt_index,
            base_code_hash=address.sha256,
            code_manifest_json=manifest,
        ))
    return manifest


def add_environment_check(app, ids, experiment_id, revision_id, decision="ready", requirements=None):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        row = EnvironmentCheckRecord(
            project_id=ids["project"], experiment_id=experiment_id,
            revision_id=revision_id, decision=decision, checker_version="test",
            requirements_json=requirements if requirements is not None else {"sandbox": "required"},
            items_json=[], suggestions_json=[],
        )
        db.add(row)
        db.flush()
        return row.id


def create_run(client, ids, revision, confirmation_id, *, key="run"):
    return client.post(
        f"/api/v1/projects/{ids['project']}/runs", headers=headers(key),
        json={"revision_id": revision["id"], "confirmation_id": confirmation_id, "mode": "local-cpu"},
    )


def test_sandbox_run_requires_model_code_attempt(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_sandbox_revision(client, ids)
        add_environment_check(app, ids, experiment_id, revision["id"])
        response = create_run(client, ids, revision, confirmation_id, key="no-code")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SANDBOX_CODE_REQUIRED"


def test_sandbox_pipeline_runs_generated_code_and_reports_truthfully(seeded):
    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_sandbox_revision(client, ids)
        manifest = add_model_attempt(app, store, experiment_id, GUEST_CODE)
        add_environment_check(app, ids, experiment_id, revision["id"])
        created = create_run(client, ids, revision, confirmation_id, key="sbx-run")
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]
        assert created.json()["data"]["status"] == "queued"

        assert demo_runtime.process_next_demo_job(factory, store, worker_id="sbx-test")
        with factory() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "completed"
            assert run.target_json == {"kind": "local-cpu", "runner": "local-sandbox-v1"}
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
            assert execution.handle.startswith("sandbox:"), execution.handle
            evaluation = db.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id))
        assert evaluation is not None
        assert evaluation.evaluator_version == "sandbox-python-evaluator-v1"
        assert evaluation.metrics_json["total"] == 20
        assert 0.0 <= evaluation.metrics_json["accuracy"] <= 1.0

        # The run is bound to the exact model attempt whose bytes executed.
        with factory() as db:
            bound = db.scalar(select(RunEventRecord).where(
                RunEventRecord.run_id == run_id, RunEventRecord.type == CODE_BOUND_EVENT_TYPE))
        assert bound is not None
        assert bound.payload_json["attempt_id"]
        assert bound.payload_json["code_sha256"] == manifest["code_sha256"]
        assert bound.payload_json["generator_model"] == "test-generator"

        # The audit listings name the executed code attempt, not only the
        # template slot: the model attempt appears with run_id NULL.
        attempts = client.get(f"/api/v1/runs/{run_id}/attempts", headers={"X-Actor-ID": "owner"})
        assert attempts.status_code == 200, attempts.text
        attempt_rows = attempts.json()["data"]
        assert any(a["source"] == "template" and a["run_id"] == run_id for a in attempt_rows)
        model_rows = [a for a in attempt_rows if a["source"] == "model"]
        assert len(model_rows) == 1 and model_rows[0]["run_id"] is None
        manifest_view = client.get(f"/api/v1/runs/{run_id}/manifest", headers={"X-Actor-ID": "owner"})
        assert manifest_view.status_code == 200, manifest_view.text
        manifest_attempts = manifest_view.json()["data"]["attempts"]
        assert any(a["source"] == "model" and a["run_id"] is None for a in manifest_attempts)

        with factory() as db:
            event = db.scalar(select(RunEventRecord).where(
                RunEventRecord.run_id == run_id, RunEventRecord.type == EVIDENCE_EVENT_TYPE))
        assert event is not None
        entry = event.payload_json["evidence"][0]
        output = json.loads(store.open_bytes(entry["sha256"]))
        assert output["evidence_class"] == "sandboxed_model_code"
        assert output["recipe"] == {"id": "sandbox_python", "version": "1"}
        assert output["sandbox"]["exit_code"] == 0
        assert isinstance(output["sandbox"]["network_isolated"], bool)
        assert len(output["sandbox"]["code_sha256"]) == 64

        # Registered evidence is readable through the artifact endpoints
        # before any export registers it: verified bytes, honest filename.
        pre_export = client.get(f"/api/v1/artifacts/{entry['sha256']}/download", headers={"X-Actor-ID": "owner"})
        assert pre_export.status_code == 200, pre_export.text
        assert pre_export.headers["content-type"].startswith("application/json")
        assert 'filename="evidence.json"' in pre_export.headers["content-disposition"]
        assert hashlib.sha256(pre_export.content).hexdigest() == entry["sha256"]
        preview = client.get(f"/api/v1/artifacts/{entry['sha256']}/text", headers={"X-Actor-ID": "owner"})
        assert preview.status_code == 200, preview.text
        assert json.loads(preview.json()["data"]["text"])["evidence_class"] == "sandboxed_model_code"
        missing = client.get(f"/api/v1/artifacts/{'0' * 64}/download", headers={"X-Actor-ID": "owner"})
        assert missing.status_code == 404, missing.text

        build = client.post(f"/api/v1/runs/{run_id}/report-builds", headers=headers("sbx-report"), json={})
        assert build.status_code == 202, build.text
        with factory() as db:
            snapshot = db.get(ReportBuildRecord, build.json()["data"]["build_id"]).input_json
        assert snapshot["evidence_class"] == "sandboxed_model_code"
        assert snapshot["limitations"] == SANDBOX_LIMITATIONS
        assert "controlled smoke" not in json.dumps(snapshot)

        from p2r_orchestration.report_jobs import process_next_report_job
        assert process_next_report_job(factory)
        report = client.get(f"/api/v1/runs/{run_id}/report", headers={"X-Actor-ID": "owner"})
        assert report.status_code == 200, report.text
        content = report.json()["data"]
        assert content["evidence_class"] == "sandboxed_model_code"
        assert content["metrics_source"] == "sandboxed_model_code_evaluation"
        assert content["evaluator_version"] == "sandbox-python-evaluator-v1"
        assert content["limitations"] == SANDBOX_LIMITATIONS


def test_failing_generated_code_fails_the_run_without_report(seeded):
    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_sandbox_revision(client, ids, key="fail")
        add_model_attempt(app, store, experiment_id, FAILING_CODE)
        add_environment_check(app, ids, experiment_id, revision["id"])
        created = create_run(client, ids, revision, confirmation_id, key="fail-run")
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]

        assert demo_runtime.process_next_demo_job(factory, store, worker_id="sbx-fail")
        with factory() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "failed"
            assert run.error_json is not None
        build = client.post(f"/api/v1/runs/{run_id}/report-builds", headers=headers("fail-report"), json={})
        assert build.status_code in (409, 422), build.text


def test_timeout_generated_code_fails_the_run(seeded):
    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_sandbox_revision(
            client, ids, training={"max_seconds": 10}, key="slow")
        add_model_attempt(app, store, experiment_id, SLEEPING_CODE)
        add_environment_check(app, ids, experiment_id, revision["id"])
        created = create_run(client, ids, revision, confirmation_id, key="slow-run")
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]

        assert demo_runtime.process_next_demo_job(factory, store, worker_id="sbx-slow")
        with factory() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "failed"
            # The shared failure path keeps the public error bounded; the
            # specific wall-clock reason is asserted at the recipe unit level.
            assert run.error_json == {"code": "PIPELINE_FAILED", "message": "The controlled pipeline failed."}


def test_environment_check_reports_sandbox_capability_item(seeded):
    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_sandbox_revision(client, ids, key="env")
        check_id = add_environment_check(app, ids, experiment_id, revision["id"], requirements={})
        with factory() as db:
            row = db.get(EnvironmentCheckRecord, check_id)
            assert row.decision == "ready"
        # The checker itself reports the sandbox capability item honestly.
        from p2r_orchestration.environment_checker import DefaultEnvironmentProbe
        items = DefaultEnvironmentProbe().check({"sandbox": "required"})
        item = next(entry for entry in items if entry.key == "sandbox")
        assert item.required is True
        assert item.status.value in ("pass", "fail", "unknown")
        if item.status.value != "pass":
            assert item.suggestion
