from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from p2r_contracts.common import (
    ApiError,
    ErrorEnvelope,
    FieldError,
    OkEnvelope,
    canonical_json,
    sha256_hex,
)
from p2r_contracts.sessions import (
    ActionRead,
    ActionStatus,
    CellCreate,
    CellRead,
    CellStatus,
    CheckpointCreate,
    CheckpointRead,
    EventRead,
    PermissionDecision,
    RecoveryStatus,
    SessionCreate,
    SessionInfo,
    SessionStatus,
    TrustClass,
    is_valid_action_transition,
    is_valid_cell_transition,
    is_valid_recovery_status,
    is_valid_session_transition,
)
from p2r_contracts.common import Ok


def test_canonical_json_is_stable_and_hashable():
    assert canonical_json({"b": 1, "a": "中文"}) == '{"a":"中文","b":1}'
    assert sha256_hex({"b": 1, "a": "中文"}) == sha256_hex('{"a":"中文","b":1}')


def test_canonical_json_covers_numbers_unicode_and_key_order():
    left = {"z": [1, 1.5, -2], "text": "é/中文"}
    right = {"text": "é/中文", "z": [1, 1.5, -2]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json({"n": 1.0}) == '{"n":1.0}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": value})


def test_envelopes_and_error_dto():
    ok = OkEnvelope(data={"value": 1}, request_id="req-1")
    assert ok.meta.request_id == "req-1"
    error = ErrorEnvelope(
        error=ApiError(
            code="INVALID",
            message="bad input",
            retryable=False,
            details={"field_errors": [FieldError(path="/name", code="required", message="required")]},
        ),
        request_id="req-1",
    )
    assert error.error.details["field_errors"][0].path == "/name"


def test_session_dtos_round_trip_and_public_shape():
    now = datetime.now(timezone.utc)
    info = SessionInfo(
        user={"id": "u1", "display_name": "Ada"},
        session={"expires_at": now, "idle_expires_at": now},
        csrf_token="csrf-secret",
        permissions={"can_create_project": True},
        memberships=[{"project_id": "p1", "role": "owner"}],
    )
    assert set(info.model_dump()) == {"user", "session", "csrf_token", "permissions", "memberships"}
    assert info.user.id == "u1"


def test_session_dtos_reject_legacy_flat_or_extra_fields():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        SessionInfo(user_id="u1", display_name="Ada", expires_at=now, idle_expires_at=now)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SessionInfo(
            user={"id": "u1", "display_name": "Ada"},
            session={"expires_at": now, "idle_expires_at": now},
            csrf_token="csrf",
            permissions={"can_create_project": True},
            memberships=[],
            status="active",
        )


def test_science_contracts_validate_bounds_hashes_and_allowlist():
    session = SessionCreate(user_id="u1", project_id="p1", trust_class=TrustClass.UNTRUSTED)
    assert session.user_id == "u1"
    with pytest.raises(ValidationError):
        CellCreate(session_id="s1", source="x" * (256 * 1024 + 1), requested_capabilities=["filesystem"])
    cell = CellCreate(session_id="s1", source="print(1)", requested_capabilities=["python"])
    assert cell.source == "print(1)"
    with pytest.raises(ValidationError):
        CellCreate(session_id="s1", source="x", requested_capabilities=["network"])
    with pytest.raises(ValidationError):
        CellRead(id="c1", session_id="s1", status=CellStatus.SUCCEEDED, source_hash="bad", stdout="", stderr="", result_summary="", namespace_hash="0" * 64, error_code=None, artifact_ids=[], environment_id="e1", created_at=datetime.now(timezone.utc))


def test_science_contracts_state_transitions_and_dtos():
    assert is_valid_session_transition(SessionStatus.ACTIVE, SessionStatus.EXPIRED)
    assert not is_valid_session_transition(SessionStatus.EXPIRED, SessionStatus.ACTIVE)
    assert is_valid_cell_transition(CellStatus.RUNNING, CellStatus.TIMED_OUT)
    assert not is_valid_cell_transition(CellStatus.SUCCEEDED, CellStatus.RUNNING)
    assert is_valid_action_transition(ActionStatus.PENDING, ActionStatus.REJECTED)
    assert is_valid_recovery_status(RecoveryStatus.PARTIAL)
    now = datetime.now(timezone.utc)
    action = ActionRead(sequence_no=1, kind="run", status=ActionStatus.SUCCEEDED, payload_hash="0"*64, actor_id="u1", cell_id="c1", artifact_ids=[], permission_decision=PermissionDecision.ALLOW, created_at=now)
    assert action.sequence_no == 1
    checkpoint = CheckpointCreate(name="cp", action_cursor=1, namespace_snapshot_hash="0"*64, input_artifact_ids=[], environment_id="e1", replay_plan={}, manifest_hash="0"*64)
    assert CheckpointRead(id="cp1", session_id="s1", status="ready", reasons=[], created_at=now, **checkpoint.model_dump()).name == "cp"
    assert EventRead(sequence_no=1, type="cell.completed", payload={"ok": True}, created_at=now).type == "cell.completed"
    assert Ok(data=action).data.sequence_no == 1
