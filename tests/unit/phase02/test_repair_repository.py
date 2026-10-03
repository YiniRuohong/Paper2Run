import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "orchestration"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "codegen"))

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import AttemptRecord, ExtractionRecord, ExperimentRecord, PaperRecord, ProjectRecord, RunRecord, SpecRevisionRecord, UserRecord
from p2r_orchestration.repair_repository import create_attempt, create_repair_proposal
from p2r_codegen import audit_payload

H = "a" * 64
P = "b" * 64

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        user = UserRecord(id="u", display_name="u")
        session.add(user); session.flush()
        project = ProjectRecord(id="p", name="p", created_by="u")
        session.add(project); session.flush()
        paper = PaperRecord(id="paper", project_id="p", sha256=H, artifact_key="x", bytes=1, page_count=1)
        session.add(paper); session.flush()
        extraction = ExtractionRecord(id="ext", paper_id="paper", content_sha256=H, payload_json={})
        session.add(extraction); session.flush()
        exp = ExperimentRecord(id="exp", project_id="p", paper_id="paper", extraction_id="ext", candidate_experiment_id="c", recipe_id="r", recipe_version="1")
        session.add(exp); session.flush()
        revision = SpecRevisionRecord(id="rev", experiment_id="exp", revision_no=1, spec_hash=H, policy_hash=P, spec_json={}, validation_json={}, comparison_json={}, budget_json={})
        session.add(revision); session.flush()
        session.add_all([
            RunRecord(id="run_a", project_id="p", experiment_id="exp", revision_id="rev", manifest_hash=H, status="failed"),
            RunRecord(id="run_b", project_id="p", experiment_id="exp", revision_id="rev", manifest_hash=H, status="failed"),
        ])
    with factory() as session:
        yield session


def make_attempt(db, run="run_a"):
    return create_attempt(db, run_id=run, attempt_index=1, source="repair", repair_ordinal=1, base_code_hash=H,
                          validation_json={"ok": False}, code_manifest_json={"files": []})


def test_create_attempt_and_cross_run_rejection(db):
    attempt = make_attempt(db)
    assert attempt.id.startswith("att_")
    with pytest.raises(ValueError, match="does not belong"):
        create_repair_proposal(db, run_id="run_b", attempt_id=attempt.id, repair_ordinal=1,
                               base_code_hash=H, patch_hash=P,
                               proposal_json=audit_payload(source="repair", base_hash=H, patch_hash=P,
                                                           validation={"ok": True}, repair_ordinal=1),
                               validation={"ok": True})


def test_closed_schema_duplicate_and_needs_revision(db):
    attempt = make_attempt(db)
    proposal = audit_payload(source="repair", base_hash=H, patch_hash=P,
                             validation={"ok": True}, repair_ordinal=1)
    row = create_repair_proposal(db, run_id="run_a", attempt_id=attempt.id, repair_ordinal=1,
                                 base_code_hash=H, patch_hash=P, proposal_json=proposal,
                                 validation={"ok": True}, status="needs_revision")
    assert row.status == "needs_revision"
    assert db.scalar(__import__("sqlalchemy").select(AttemptRecord).where(AttemptRecord.id == attempt.id)).patch_hash is None
    with pytest.raises(ValueError, match="already proposed"):
        create_repair_proposal(db, run_id="run_a", attempt_id=attempt.id, repair_ordinal=1,
                               base_code_hash=H, patch_hash=P, proposal_json=proposal,
                               validation={"ok": True})
    with pytest.raises(ValueError, match="does not match"):
        create_repair_proposal(db, run_id="run_a", attempt_id=attempt.id, repair_ordinal=2,
                               base_code_hash=H, patch_hash=P,
                               proposal_json={**proposal, "repair_ordinal": 2}, validation={"ok": True})


def test_secret_and_invalid_validation_rejected(db):
    attempt = make_attempt(db)
    with pytest.raises(ValueError):
        create_repair_proposal(db, run_id="run_a", attempt_id=attempt.id, repair_ordinal=1,
                               base_code_hash=H, patch_hash=P,
                               proposal_json={"source": "repair", "api_key": "sk-abcdefghijklmnop"},
                               validation={"ok": True})
    with pytest.raises(ValueError):
        create_repair_proposal(db, run_id="run_a", attempt_id=attempt.id, repair_ordinal=1,
                               base_code_hash=H, patch_hash=P,
                               proposal_json={}, validation={"ok": True, "unknown": True})
