import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "orchestration"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "codegen"))

from p2r_api.main import create_app
from p2r_persistence.models import AttemptRecord, ExperimentRecord, IdempotencyKey, Job, MembershipRecord, ProjectRecord, RepairProposalRecord, RunRecord, UserRecord
from p2r_codegen import audit_payload

H = "a" * 64
P = "b" * 64

@pytest.fixture
def app(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'repair.sqlite3'}", supervisor=object(), secure_cookies=False)
    with app.state.runtime["session_factory"].begin() as db:
        user = UserRecord(id="owner", display_name="owner", can_create_project=True)
        db.add(user); db.flush()
        project = ProjectRecord(id="project", name="project", created_by="owner")
        other = ProjectRecord(id="other", name="other", created_by="owner")
        db.add_all([project, other]); db.flush()
        db.add_all([MembershipRecord(project_id="project", user_id="owner", role="owner"), MembershipRecord(project_id="other", user_id="owner", role="owner")]); db.flush()
        from p2r_persistence.models import PaperRecord, ExtractionRecord
        paper = PaperRecord(id="paper", project_id="project", sha256=H, artifact_key="x", bytes=1, page_count=1)
        db.add(paper); db.flush()
        extraction = ExtractionRecord(id="ext", paper_id="paper", content_sha256=H, payload_json={})
        db.add(extraction); db.flush()
        exp = ExperimentRecord(id="exp", project_id="project", paper_id="paper", extraction_id="ext", candidate_experiment_id="c", recipe_id="r", recipe_version="1", status="active")
        db.add(exp); db.flush()
        from p2r_persistence.models import SpecRevisionRecord
        revision = SpecRevisionRecord(id="rev", experiment_id="exp", revision_no=1, spec_hash=H, policy_hash=P, spec_json={}, validation_json={}, comparison_json={}, budget_json={})
        db.add(revision); db.flush()
        run = RunRecord(id="run", project_id="project", experiment_id="exp", revision_id="rev", manifest_hash=H, status="failed")
        db.add_all([exp, run]); db.flush()
        db.add(AttemptRecord(id="att_test", run_id="run", attempt_index=1, source="repair", repair_ordinal=1, status="candidate", base_code_hash=H, validation_json={}, code_manifest_json={}))
    return app


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def payload(extra=None):
    audit = audit_payload(source="repair", base_hash=H, patch_hash=P, validation={"ok": True}, repair_ordinal=1)
    if extra:
        audit.update(extra)
    return {"attempt_id": "att_test", "repair_ordinal": 1, "base_code_hash": H, "patch_hash": P, "proposal_json": audit, "validation": {"ok": True}}


def test_http_idempotency_cross_project_and_list_audit_only(app):
    with TestClient(app, base_url="https://testserver") as client:
        url = "/api/v1/runs/run/repair-proposals"
        first = client.post(url, headers=headers("repair-key"), json=payload())
        replay = client.post(url, headers=headers("repair-key"), json=payload())
        conflict = client.post(url, headers=headers("repair-key"), json=payload({"repair_ordinal": 2}))
        assert first.status_code == 202, first.text
        assert replay.status_code == 202
        assert replay.json()["data"] == first.json()["data"]
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        listed = client.get(url, headers={"X-Actor-ID": "owner"})
        assert listed.status_code == 200
        assert listed.json()["data"][0]["proposal_id"] == first.json()["data"]["proposal_id"]
        assert "proposal_json" not in listed.json()["data"][0]
        assert client.get("/api/v1/runs/run/repair-proposals", headers={"X-Actor-ID": "owner"}).status_code == 200


def test_accepted_repair_proposal_enqueues_repair_job(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/runs/run/repair-proposals",
            headers=headers("accepted-repair"),
            json={**payload(), "status": "accepted"},
        )
        assert response.status_code == 202, response.text
        replay = client.post(
            "/api/v1/runs/run/repair-proposals",
            headers=headers("accepted-repair"),
            json={**payload(), "status": "accepted"},
        )
        assert replay.status_code == 202
        assert replay.json()["data"] == response.json()["data"]

    with app.state.runtime["session_factory"]() as db:
        jobs = db.scalars(select(Job).where(Job.kind == "repair_attempt")).all()
        assert len(jobs) == 1
        assert jobs[0].payload_ref == response.json()["data"]["proposal_id"]
        assert jobs[0].idempotency_key == f"repair:{jobs[0].payload_ref}"
        assert db.get(ExperimentRecord, "exp").repairs_used == 1
        assert len(db.scalars(select(RepairProposalRecord)).all()) == 1


@pytest.mark.parametrize("status", ["proposed", "rejected", "needs_revision"])
def test_unaccepted_proposal_does_not_enqueue(app, status):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/v1/runs/run/repair-proposals",
                               headers=headers(status), json={**payload(), "status": status})
        assert response.status_code == 202, response.text
    with app.state.runtime["session_factory"]() as db:
        assert db.scalars(select(Job)).all() == []


def test_enqueue_failure_rolls_back_proposal_budget_and_idempotency(app, monkeypatch):
    from p2r_persistence.job_repository import JobRepository

    def fail_enqueue(self, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(JobRepository, "enqueue", fail_enqueue)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        response = client.post("/api/v1/runs/run/repair-proposals",
                               headers=headers("enqueue-failure"),
                               json={**payload(), "status": "accepted"})
        assert response.status_code == 500
    with app.state.runtime["session_factory"]() as db:
        assert db.get(ExperimentRecord, "exp").repairs_used == 0
        assert db.scalars(select(RepairProposalRecord)).all() == []
        assert db.scalars(select(Job)).all() == []
        assert db.scalars(select(IdempotencyKey)).all() == []


def test_exhausted_budget_does_not_persist_proposal_or_job(app):
    with app.state.runtime["session_factory"].begin() as db:
        exp = db.get(ExperimentRecord, "exp")
        exp.repairs_used = exp.repairs_limit
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/v1/runs/run/repair-proposals",
                               headers=headers("exhausted"),
                               json={**payload(), "status": "accepted"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REPAIR_BUDGET_EXCEEDED"
    with app.state.runtime["session_factory"]() as db:
        assert db.scalars(select(RepairProposalRecord)).all() == []
        assert db.scalars(select(Job)).all() == []


def test_http_forbids_extra_fields_and_cross_project_404(app):
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/v1/runs/run/repair-proposals", headers=headers("bad"), json={**payload(), "extra": True})
        assert response.status_code == 422
        missing = client.post("/api/v1/runs/not-owned/repair-proposals", headers=headers("missing"), json=payload())
        assert missing.status_code == 404
