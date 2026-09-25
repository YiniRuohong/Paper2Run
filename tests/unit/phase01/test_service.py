from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from p2r_artifacts.store import LocalArtifactStore
from p2r_contracts.sessions import Capability, CellCreate, CellStatus, CheckpointCreate, SessionStatus
from p2r_orchestration.service import (
    ConflictError,
    InvalidStateError,
    OrchestrationService,
    PermissionDeniedError,
)
from p2r_persistence.db import create_engine, init_db
from p2r_persistence.models import Action, Artifact, CellExecution, Environment, Event, IdempotencyKey, SessionRecord
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass
class FakeResult:
    stdout: str = "hello\n"
    stderr: str = "warning\n"
    result_summary: str = "42"
    namespace_hash: str = "a" * 64
    error_code: str | None = None


class FakeSupervisor:
    def __init__(self, result: FakeResult | None = None):
        self.result = result or FakeResult()
        self.calls: list[object] = []

    def execute(self, request: object) -> FakeResult:
        self.calls.append(request)
        return self.result


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'service.sqlite3'}")
    init_db(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def service(db, tmp_path):
    return OrchestrationService(db, FakeSupervisor(), LocalArtifactStore(tmp_path / "artifacts"))


def make_cell(session_id: str, source: str = "print('hello')", capabilities=None) -> CellCreate:
    return CellCreate(
        session_id=session_id,
        source=source,
        requested_capabilities=list(capabilities or []),
    )


def body(cell: CellCreate) -> str:
    return cell.model_dump_json()


def test_create_get_list_session(db):
    service = OrchestrationService(db)
    first = service.create_session("user-1", {"role": "editor"}, trust_class="trusted", project_id="p1")
    second = service.create_session("user-2", {"role": "viewer"})

    assert service.get_session(first.id).id == first.id
    assert first.status == SessionStatus.ACTIVE.value
    assert first.metadata_json["trust_class"] == "trusted"
    assert [row.id for row in service.list_sessions(limit=1)] == sorted([first.id, second.id])[:1]
    assert [row.id for row in service.list_sessions(user_id="user-2")] == [second.id]


def test_submit_allowed_cell_persists_outputs_environment_event_and_action(service, db):
    session = service.create_session("user-1", {"capabilities": [Capability.PYTHON.value]})
    cell = make_cell(session.id, capabilities=[Capability.PYTHON])

    result = service.submit_cell(session.id, "actor-1", cell, "key-1", body(cell))

    assert result.status == CellStatus.SUCCEEDED.value
    assert result.stdout == "hello\n"
    assert result.stderr == "warning\n"
    assert result.namespace_hash == "a" * 64
    assert result.environment_id
    execution = db.scalar(select(CellExecution).where(CellExecution.id == result.id))
    assert execution.status == CellStatus.SUCCEEDED.value
    assert execution.output_json["result_summary"] == "42"
    assert db.scalar(select(Environment).where(Environment.id == result.environment_id)) is not None

    actions = service.list_actions(session.id)
    events = service.get_events(session.id)
    assert len(actions) == 1
    assert actions[0].sequence_no == 0
    assert actions[0].kind == "cell.submit"
    assert actions[0].actor_id == "actor-1"
    assert len(actions[0].payload_hash) == 64
    assert events[0].sequence_no == 0
    assert events[0].type == "cell.completed"
    assert events[0].payload["cell_id"] == result.id

    artifacts = list(db.scalars(select(Artifact).where(Artifact.session_id == session.id)))
    assert {artifact.metadata_json["stream"] for artifact in artifacts} == {"stdout", "stderr"}
    assert set(result.artifact_ids) == {artifact.id for artifact in artifacts}


def test_submit_claim_result_controls_in_progress_without_stale_pre_read(service, db, monkeypatch):
    session = service.create_session("user-1")
    cell = make_cell(session.id, "1 + 1")
    record = IdempotencyKey(scope=f"cell:{session.id}:actor-1", key="same-key", body_hash="a" * 64)

    class Claim:
        conflict = False
        def __init__(self):
            self.record = record

    class Repository:
        def __init__(self, _db):
            pass
        def claim(self, **_kwargs):
            return Claim()

    monkeypatch.setattr("p2r_orchestration.service.IdempotencyRepository", Repository)
    with pytest.raises(Exception) as exc:
        service.submit_cell(session.id, "actor-1", cell, "same-key", body(cell))
    assert exc.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert service.supervisor.calls == []

    monkeypatch.undo()
    session = service.create_session("user-1")
    first_cell = make_cell(session.id, "1 + 1")
    first = service.submit_cell(session.id, "actor-1", first_cell, "same-key", body(first_cell))
    second = service.submit_cell(session.id, "actor-1", first_cell, "same-key", body(first_cell))

    assert second == first
    assert db.scalar(select(func.count(Action.id)).where(Action.session_id == session.id)) == 1
    with pytest.raises(ConflictError) as exc:
        service.submit_cell(session.id, "actor-1", make_cell(session.id, "2 + 2"), "same-key", "different")
    assert exc.value.code == "CONFLICT"


@pytest.mark.parametrize(
    ("metadata", "status", "capabilities", "error"),
    [
        ({"role": "viewer"}, "active", None, PermissionDeniedError),
        ({"read_only": True}, "active", None, PermissionDeniedError),
        ({}, "revoked", None, InvalidStateError),
        ({"capabilities": [Capability.PYTHON.value]}, "active", [Capability.PLOT], PermissionDeniedError),
    ],
)
def test_submit_rejects_viewers_inactive_and_disallowed_capabilities(db, metadata, status, capabilities, error):
    service = OrchestrationService(db, FakeSupervisor())
    session = service.create_session("user-1", metadata)
    session.status = status
    db.commit()
    cell = make_cell(session.id, capabilities=capabilities)

    with pytest.raises(error):
        service.submit_cell(session.id, "actor-1", cell, "key", body(cell))
    assert db.scalar(select(func.count(Action.id)).where(Action.session_id == session.id)) == 0


def test_timeout_maps_to_timed_out(service, db):
    service.supervisor = FakeSupervisor(FakeResult(stdout="", stderr="", namespace_hash="b" * 64, error_code="CELL_TIMEOUT"))
    session = service.create_session("user-1")
    cell = make_cell(session.id, "while True: pass")

    result = service.submit_cell(session.id, "actor-1", cell, "timeout", body(cell))

    assert result.status == CellStatus.TIMED_OUT.value
    assert result.error_code == "CELL_TIMEOUT"
    assert service.get_events(session.id)[0].payload["status"] == CellStatus.TIMED_OUT.value


def test_action_event_order_and_bounded_limits(service):
    session = service.create_session("user-1")
    for index in range(3):
        cell = make_cell(session.id, str(index))
        service.submit_cell(session.id, "actor-1", cell, f"key-{index}", body(cell))

    assert [item.sequence_no for item in service.list_actions(session.id, limit=2)] == [0, 1]
    assert [item.sequence_no for item in service.list_actions(session.id, after=0, limit=1)] == [1]
    assert [item.sequence_no for item in service.get_events(session.id, limit=2)] == [0, 1]
    assert [item.sequence_no for item in service.get_events(session.id, after=1, limit=1)] == [2]
    with pytest.raises(Exception):
        service.list_actions(session.id, limit=201)


def test_recovery_classifies_tampered_manifest_artifact_and_invalid_state(db, tmp_path):
    service = OrchestrationService(db, artifact_store=LocalArtifactStore(tmp_path / "artifacts"))
    session = service.create_session("user-1")
    environment = Environment(session_id=session.id, name="runtime", spec={})
    service.db.add(environment)
    service.db.flush()
    checkpoint = service.create_checkpoint(session.id, CheckpointCreate(
        name="baseline", action_cursor=0, namespace_snapshot_hash="0" * 64,
        environment_id=environment.id, replay_plan={}, manifest_hash="0" * 64,
    ))
    artifact = db.get(Artifact, checkpoint.manifest_artifact_id)
    artifact_path = tmp_path / "artifacts" / artifact.sha256[:2] / artifact.sha256
    artifact_path.write_text("tampered")
    status = service.classify_recovery(session.id, checkpoint.id)
    assert status.status == "failed"
    assert "manifest_artifact_hash_mismatch" in status.reasons


def test_checkpoint_rejects_action_cursor_gap(db):
    service = OrchestrationService(db)
    session = service.create_session("user-1")
    environment = Environment(session_id=session.id, name="runtime", spec={})
    service.db.add(environment)
    service.db.flush()
    with pytest.raises(Exception, match="action_cursor"):
        service.create_checkpoint(session.id, CheckpointCreate(
            name="gap", action_cursor=2, namespace_snapshot_hash="0" * 64,
            environment_id=environment.id, replay_plan={}, manifest_hash="0" * 64,
        ))


def test_checkpoint_manifest_is_stored_as_verified_artifact_and_recovery_classifies_missing_restart(db, tmp_path):
    service = OrchestrationService(db, artifact_store=LocalArtifactStore(tmp_path / "artifacts"))
    session = service.create_session("user-1")
    environment = Environment(session_id=session.id, name="runtime", spec={"python": "3.12"})
    service.db.add(environment)
    service.db.flush()
    manifest = {
        "action_cursor": 0,
        "input_artifact_ids": [],
        "environment_id": environment.id,
    }
    checkpoint = CheckpointCreate(
        name="baseline",
        action_cursor=0,
        namespace_snapshot_hash="c" * 64,
        environment_id=environment.id,
        replay_plan={"steps": []},
        manifest=manifest,
        manifest_hash=__import__("p2r_contracts.common", fromlist=["sha256_hex"]).sha256_hex(manifest),
    )
    created = service.create_checkpoint(session.id, checkpoint)
    status = service.classify_recovery(session.id, created.id)
    assert status.status == "partial"
    assert "supervisor_restart_unavailable" in status.reasons
    assert created.manifest_artifact_id
    artifact = db.get(Artifact, created.manifest_artifact_id)
    assert artifact is not None
    assert artifact.sha256 == created.manifest_hash


def test_restore_rejects_invalid_manifest_hash(db):
    service = OrchestrationService(db)
    session = service.create_session("user-1")
    with pytest.raises(Exception, match="manifest_hash"):
        service.create_checkpoint(session.id, CheckpointCreate(
            name="bad", action_cursor=0, namespace_snapshot_hash="0" * 64,
            environment_id="missing", replay_plan={}, manifest={"x": 1}, manifest_hash="f" * 64,
        ))


def test_checkpoint_create_and_list(service):
    session = service.create_session("user-1")
    environment = Environment(session_id=session.id, name="runtime", spec={})
    service.db.add(environment)
    service.db.flush()
    service.db.add(Action(session_id=session.id, sequence_no=0, action_type="cell.submit", payload={"actor_id": "actor-1"}))
    service.db.commit()
    checkpoint = CheckpointCreate(
        name="baseline",
        action_cursor=0,
        namespace_snapshot_hash="c" * 64,
        environment_id=environment.id,
        replay_plan={"steps": ["cell-1"]},
        manifest_hash="d" * 64,
    )

    created = service.create_checkpoint(session.id, checkpoint)
    listed = service.list_checkpoints(session.id)

    assert created.id == listed[0].id
    assert listed[0].name == "baseline"
    assert listed[0].replay_plan == {"steps": ["cell-1"]}
