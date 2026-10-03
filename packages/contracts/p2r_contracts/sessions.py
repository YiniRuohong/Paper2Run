from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import _validate_json

_ID = Field(min_length=1, max_length=256)
_HASH = Field(pattern=r"^[0-9a-fA-F]{64}$")


class SessionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ActionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class CellStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TrustClass(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


class RecoveryStatus(StrEnum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    FAILED = "failed"


class Capability(StrEnum):
    PYTHON = "python"
    PLOT = "plot"
    DATAFRAME = "dataframe"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(_Contract):
    user_id: str = _ID
    project_id: str = _ID
    trust_class: TrustClass


class SessionUser(_Contract):
    id: str = _ID
    display_name: str = Field(min_length=1, max_length=256)


class SessionWindow(_Contract):
    expires_at: datetime
    idle_expires_at: datetime


class SessionPermissions(_Contract):
    can_create_project: bool


class SessionMembership(_Contract):
    project_id: str = _ID
    role: str = Field(min_length=1, max_length=128)


class SessionInfo(_Contract):
    user: SessionUser
    session: SessionWindow
    csrf_token: str = Field(min_length=1, max_length=4096)
    permissions: SessionPermissions
    memberships: list[SessionMembership]


class CellCreate(_Contract):
    session_id: str = _ID
    source: str = Field(min_length=1, max_length=256 * 1024)
    base_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=256)
    requested_capabilities: list[Capability] = Field(default_factory=list, max_length=32)

    def model_post_init(self, __context: Any) -> None:
        _validate_json(self.model_dump(mode="json"))


class CellRead(_Contract):
    id: str = _ID
    session_id: str = _ID
    status: CellStatus
    source_hash: str = _HASH
    stdout: str = Field(max_length=256 * 1024)
    stderr: str = Field(max_length=256 * 1024)
    result_summary: str = Field(max_length=16 * 1024)
    namespace_hash: str = _HASH
    error_code: str | None = Field(default=None, max_length=256)
    artifact_ids: list[str] = Field(default_factory=list, max_length=256)
    environment_id: str = _ID
    created_at: datetime


class ActionRead(_Contract):
    sequence_no: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=128)
    status: ActionStatus
    payload_hash: str = _HASH
    actor_id: str = _ID
    cell_id: str | None = Field(default=None, min_length=1, max_length=256)
    artifact_ids: list[str] = Field(default_factory=list, max_length=256)
    permission_decision: PermissionDecision
    created_at: datetime


class CheckpointCreate(_Contract):
    name: str = Field(min_length=1, max_length=256)
    action_cursor: int = Field(ge=0)
    namespace_snapshot_hash: str = _HASH
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=256)
    environment_id: str = _ID
    replay_plan: dict[str, Any]
    manifest_hash: str = _HASH
    manifest: dict[str, Any] | None = None

    def model_post_init(self, __context: Any) -> None:
        _validate_json(self.replay_plan)
        if self.manifest is not None:
            _validate_json(self.manifest)


class CheckpointRead(CheckpointCreate):
    id: str = _ID
    session_id: str = _ID
    status: str = Field(min_length=1, max_length=64)
    reasons: list[str] = Field(default_factory=list, max_length=64)
    manifest_artifact_id: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime


class RecoveryRead(_Contract):
    status: RecoveryStatus
    reasons: list[str] = Field(default_factory=list, max_length=64)
    checkpoint_id: str = _ID


class RestoreRequest(_Contract):
    checkpoint_id: str = _ID


class EventRead(_Contract):
    sequence_no: int = Field(ge=0)
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]
    created_at: datetime

    def model_post_init(self, __context: Any) -> None:
        _validate_json(self.payload)


def is_valid_session_transition(old: SessionStatus, new: SessionStatus) -> bool:
    return (old, new) in {(SessionStatus.ACTIVE, SessionStatus.EXPIRED), (SessionStatus.ACTIVE, SessionStatus.REVOKED)}


def is_valid_cell_transition(old: CellStatus, new: CellStatus) -> bool:
    return (old, new) in {
        (CellStatus.QUEUED, CellStatus.RUNNING),
        (CellStatus.QUEUED, CellStatus.CANCELLED),
        (CellStatus.RUNNING, CellStatus.SUCCEEDED),
        (CellStatus.RUNNING, CellStatus.FAILED),
        (CellStatus.RUNNING, CellStatus.TIMED_OUT),
        (CellStatus.RUNNING, CellStatus.CANCELLED),
    }


def is_valid_action_transition(old: ActionStatus, new: ActionStatus) -> bool:
    return (old, new) in {
        (ActionStatus.PENDING, ActionStatus.RUNNING),
        (ActionStatus.PENDING, ActionStatus.REJECTED),
        (ActionStatus.RUNNING, ActionStatus.SUCCEEDED),
        (ActionStatus.RUNNING, ActionStatus.FAILED),
    }


def is_valid_recovery_status(status: RecoveryStatus) -> bool:
    return status in set(RecoveryStatus)
