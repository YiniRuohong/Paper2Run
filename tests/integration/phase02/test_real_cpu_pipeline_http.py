"""Real local-CPU pipeline: Run -> real predictions -> Evaluation -> Report -> Export.

The real_cpu_baseline/1 recipe executes a genuine pure-stdlib training run on
a frozen repository dataset. Every stage downstream must tell the truth about
it: metrics come from real labels, evidence registers the runner's real
output, the report marks it real_local_cpu (never controlled smoke) while
keeping its limitations, and the export is content-addressed.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.evidence_registry import EVIDENCE_EVENT_TYPE
from p2r_orchestration.report_builder import REAL_LIMITATIONS
from p2r_orchestration.report_jobs import process_next_report_job
from p2r_orchestration.run_worker import process_next_run_job
from p2r_evaluation import evaluate_predictions
from p2r_runner import (
    EVIDENCE_CLASS_REAL_LOCAL_CPU,
    REAL_EVALUATOR_VERSION,
    REAL_RECIPE_ID,
    REAL_RECIPE_VERSION,
    run_real_cpu_baseline,
)
from p2r_persistence.models import (
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

PAPER_BYTES = b"Repository baseline dataset description. Real local training, not a controlled smoke fixture."


@pytest.fixture
def seeded(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    address = store.put_bytes(PAPER_BYTES)
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'real.sqlite3'}",
        supervisor=object(), secure_cookies=False, artifact_store=store,
    )
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="real recipe project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        paper = PaperRecord(
            project_id=project.id, title="baseline paper", status="ready",
            sha256=address.sha256, artifact_key=address.sha256,
            bytes=address.bytes, page_count=0,
            source_kind="text_fixture", media_type="text/plain",
        )
        db.add(paper)
        db.flush()
        extraction = ExtractionRecord(
            paper_id=paper.id, content_sha256=address.sha256,
            payload_json={"candidate_experiments": [
                {"id": "candidate-real", "recipe_id": "real_cpu_baseline", "recipe_version": "1",
                 "label": "real cpu baseline", "execution_support": "supported"},
            ]},
        )
        db.add(extraction)
        db.flush()
        ids = {"project": project.id, "paper": paper.id, "extraction": extraction.id}
    return app, ids, store


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def make_confirmed_real_revision(client, ids, *, key="real"):
    def hdr(suffix):
        return {"X-Actor-ID": "owner", "Idempotency-Key": f"{key}-{suffix}"}

    experiment = client.post(
        f"/api/v1/projects/{ids['project']}/experiments",
        headers=hdr("experiment"),
        json={"paper_id": ids["paper"], "extraction_id": ids["extraction"],
              "candidate_experiment_id": "candidate-real",
              "recipe_id": "real_cpu_baseline", "recipe_version": "1"},
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["experiment_id"]
    revision = client.post(
        f"/api/v1/projects/{ids['project']}/spec-revisions",
        headers=hdr("revision"),
        json={"experiment_id": experiment_id, "base_revision_id": None,
              "spec": {"schema_version": "1.0", "target": "local-cpu",
                       "training": {"seed": 7, "max_epochs": 20}}},
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


def expected_real_result():
    return run_real_cpu_baseline(
        recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION,
        parameters={"seed": 7, "max_epochs": 20},
    )


def test_real_recipe_pipeline_produces_truthful_report_and_export(seeded):
    from p2r_api import demo as demo_runtime

    app, ids, store = seeded
    factory = app.state.runtime["session_factory"]
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_real_revision(client, ids)
        add_environment_check(app, ids, experiment_id, revision["id"], "ready")
        created = client.post(
            f"/api/v1/projects/{ids['project']}/runs", headers=headers("real-run"),
            json={"revision_id": revision["id"], "confirmation_id": confirmation_id, "mode": "local-cpu"},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["run_id"]
        assert created.json()["data"]["status"] == "queued"

        # Execute through the same claim/handler path the guided demo uses,
        # with the artifact store so the real output is registered as evidence.
        assert demo_runtime.process_next_demo_job(factory, store, worker_id="real-test")
        with factory() as db:
            run = db.get(RunRecord, run_id)
            assert run.status == "completed"
            execution = db.scalar(select(ExecutionRecord).where(ExecutionRecord.run_id == run_id))
            assert execution.handle.startswith("local-cpu:"), execution.handle
            evaluation = db.scalar(select(EvaluationRecord).where(EvaluationRecord.run_id == run_id))

        # The recorded metrics are recomputed against the recipe's real labels.
        expected = expected_real_result()
        expected_evaluated = evaluate_predictions(expected["predictions"], labels=expected["labels"])
        assert evaluation.metrics_json == expected_evaluated["metrics"]
        assert evaluation.metrics_hash == expected_evaluated["metrics_hash"] == run.metrics_hash
        assert evaluation.metrics_json["total"] == 80
        assert evaluation.evaluator_version == REAL_EVALUATOR_VERSION

        # The registered evidence artifact is the runner's real output.
        with factory() as db:
            event = db.scalar(select(RunEventRecord).where(
                RunEventRecord.run_id == run_id, RunEventRecord.type == EVIDENCE_EVENT_TYPE))
        assert event is not None
        entry = event.payload_json["evidence"][0]
        output = json.loads(store.open_bytes(entry["sha256"]))
        assert hashlib.sha256(store.open_bytes(entry["sha256"])).hexdigest() == entry["sha256"]
        assert output["evidence_class"] == EVIDENCE_CLASS_REAL_LOCAL_CPU
        assert output["recipe"] == {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
        assert len(output["predictions"]) == 80

        # The report snapshot marks the run real, never controlled smoke, and
        # keeps real-recipe limitations.
        build = client.post(
            f"/api/v1/runs/{run_id}/report-builds", headers=headers("real-report"), json={})
        assert build.status_code == 202, build.text
        with factory() as db:
            snapshot = db.get(ReportBuildRecord, build.json()["data"]["build_id"]).input_json
        assert snapshot["recipe"] == {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
        assert snapshot["evidence_class"] == "real_local_cpu"
        assert snapshot["controlled_smoke"] is False
        assert snapshot["limitations"] == REAL_LIMITATIONS
        assert snapshot["limitations"]
        assert "controlled smoke" not in json.dumps(snapshot)
        assert snapshot["evaluator_version"] == REAL_EVALUATOR_VERSION
        assert snapshot["metrics"] == expected_evaluated["metrics"]

        assert process_next_report_job(factory)
        report = client.get(f"/api/v1/runs/{run_id}/report", headers={"X-Actor-ID": "owner"})
        assert report.status_code == 200, report.text
        content = report.json()["data"]
        assert content["recipe"] == {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
        assert content["metrics_source"] == "real_local_cpu_evaluation"
        assert content["evaluator_version"] == REAL_EVALUATOR_VERSION
        assert content["limitations"] == REAL_LIMITATIONS
        assert "controlled smoke" not in json.dumps(content)

        # The report_json export downloads the frozen report bytes.
        queued = client.post(
            f"/api/v1/runs/{run_id}/exports", headers=headers("real-export"),
            json={"formats": ["report_json"]})
        assert queued.status_code == 202, queued.text
        assert process_next_run_job(factory, app.state.runtime["artifact_store"])
        exports = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"})
        item = exports.json()["data"][0]
        assert item["status"] == "succeeded"
        download = client.get(
            f"/api/v1/artifacts/{item['artifact_id']}/download", headers={"X-Actor-ID": "owner"})
        assert download.status_code == 200, download.text
        assert hashlib.sha256(download.content).hexdigest() == item["artifact_id"]
        exported = json.loads(download.content)
        assert exported["run_id"] == run_id
        assert exported["recipe"] == {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
        assert exported["limitations"] == REAL_LIMITATIONS


def test_blocked_environment_check_refuses_real_recipe_run(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_real_revision(client, ids, key="blocked")
        add_environment_check(app, ids, experiment_id, revision["id"], "blocked")
        response = client.post(
            f"/api/v1/projects/{ids['project']}/runs", headers=headers("blocked-run"),
            json={"revision_id": revision["id"], "confirmation_id": confirmation_id, "mode": "local-cpu"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ENVIRONMENT_BLOCKED"
        with app.state.runtime["session_factory"]() as db:
            assert db.scalars(select(RunRecord)).all() == []


def test_real_recipe_without_ready_environment_check_is_refused(seeded):
    app, ids, store = seeded
    with TestClient(app, base_url="https://testserver") as client:
        experiment_id, revision, confirmation_id = make_confirmed_real_revision(client, ids, key="noenv")
        response = client.post(
            f"/api/v1/projects/{ids['project']}/runs", headers=headers("noenv-run"),
            json={"revision_id": revision["id"], "confirmation_id": confirmation_id, "mode": "local-cpu"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ENVIRONMENT_CHECK_REQUIRED"
