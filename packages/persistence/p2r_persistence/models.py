from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BIGINT, JSON, DDL, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint, event
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC timestamps and reject ambiguous naive values."""

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class UserRecord(TimestampMixin, Base):
    __tablename__ = "identity_users"
    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="ck_identity_user_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    can_create_project: Mapped[bool] = mapped_column(nullable=False, default=False)


class AuthSessionRecord(TimestampMixin, Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("identity_users.id"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ProjectRecord(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_project_name"),
        CheckConstraint("length(description) <= 2000", name="ck_project_description"),
        CheckConstraint("version >= 1", name="ck_project_version"),
        CheckConstraint("deletion_state IN ('active', 'deleting', 'deleted')", name="ck_project_deletion_state"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(ForeignKey("identity_users.id"), nullable=False)
    version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    deletion_state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class MembershipRecord(TimestampMixin, Base):
    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_membership_project_user"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_membership_role"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("identity_users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class AuditLogRecord(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("identity_users.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


# Keep create_all audit immutability consistent with revision 0003 without
# changing the existing scientific action/event guard installer.
for operation in ("UPDATE", "DELETE"):
    event.listen(AuditLogRecord.__table__, "after_create", DDL(
        f"CREATE TRIGGER p2r_0003_audit_{operation.lower()} BEFORE {operation} ON audit_logs "
        "BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
    ).execute_if(dialect="sqlite"))
event.listen(AuditLogRecord.__table__, "after_create", DDL(
    "CREATE OR REPLACE FUNCTION p2r_0003_audit_guard() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN RAISE EXCEPTION 'append-only table'; END; $$"
).execute_if(dialect="postgresql"))
event.listen(AuditLogRecord.__table__, "after_create", DDL(
    "CREATE TRIGGER p2r_0003_audit_guard BEFORE UPDATE OR DELETE ON audit_logs "
    "FOR EACH ROW EXECUTE FUNCTION p2r_0003_audit_guard()"
).execute_if(dialect="postgresql"))


class SessionRecord(TimestampMixin, Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actions: Mapped[list[Action]] = relationship(back_populates="session")
    events: Mapped[list[Event]] = relationship(back_populates="session")


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("session_id", "sequence_no", name="uq_actions_session_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BIGINT, nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    session: Mapped[SessionRecord] = relationship(back_populates="actions")


class CellExecution(Base):
    __tablename__ = "cell_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    cell_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_text: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Environment(Base):
    __tablename__ = "environments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    manifest_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("session_id", "sequence_no", name="uq_events_session_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BIGINT, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    session: Mapped[SessionRecord] = relationship(back_populates="events")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class PaperRecord(TimestampMixin, Base):
    __tablename__ = "papers"
    __table_args__ = (CheckConstraint("status IN ('uploaded', 'parsing', 'ready', 'failed', 'unsupported')", name="ck_paper_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "pap_" + str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    page_count: Mapped[int] = mapped_column(nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="pdf", server_default="pdf")
    media_type: Mapped[str] = mapped_column(String(255), nullable=False, default="application/pdf", server_default="application/pdf")
    processing_consent: Mapped[bool] = mapped_column(nullable=False, default=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PaperVersionRecord(TimestampMixin, Base):
    __tablename__ = "paper_versions"
    __table_args__ = (UniqueConstraint("paper_id", "version", name="uq_paper_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "pv_" + str(uuid.uuid4()))
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    page_count: Mapped[int] = mapped_column(nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="pdf", server_default="pdf")
    media_type: Mapped[str] = mapped_column(String(255), nullable=False, default="application/pdf", server_default="application/pdf")


class PaperTextRecord(TimestampMixin, Base):
    __tablename__ = "paper_texts"
    __table_args__ = (CheckConstraint("status IN ('parsing', 'ready', 'failed')", name="ck_paper_text_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "ptx_" + str(uuid.uuid4()))
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    backend: Mapped[str | None] = mapped_column(String(32))
    backend_version: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(nullable=False)
    pages_parsed: Mapped[int] = mapped_column(nullable=False, default=0)
    chunk_map_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="parsing")
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SourceRefRecord(Base):
    __tablename__ = "source_refs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "sr_" + str(uuid.uuid4()))
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    extraction_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page: Mapped[int] = mapped_column(nullable=False)
    section: Mapped[str | None] = mapped_column(String(255))
    table: Mapped[str | None] = mapped_column(String(255))
    row: Mapped[str | None] = mapped_column(String(255))
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    bbox: Mapped[list[float] | None] = mapped_column(JSON)


class ExtractionRecord(TimestampMixin, Base):
    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("paper_id", "content_sha256", name="uq_extraction_paper_content"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "ext_" + str(uuid.uuid4()))
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class EnvironmentCheckRecord(Base):
    __tablename__ = "environment_checks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "env_" + str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("spec_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    checker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    requirements_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    suggestions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class SSHVerificationRecord(Base):
    """Manual SSH verification of a blocked environment check.

    Stores only the safe target projection (never credential material) and a
    safe environment summary. Verification success never creates a Run; it only
    advances to ``awaiting_confirmation`` for an explicit user confirmation.
    """

    __tablename__ = "ssh_verifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "sshv_" + str(uuid.uuid4()))
    environment_check_id: Mapped[str] = mapped_column(
        ForeignKey("environment_checks.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("spec_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="awaiting_confirmation")
    target_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class OperationRecord(TimestampMixin, Base):

    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "op_" + str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="succeeded")
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    payload_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    # Monotonically increasing lease generation. Workers must echo the value
    # they received from claim when extending or completing a job.
    fencing_token: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    leased_by: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    error: Mapped[str | None] = mapped_column(Text)


class ExperimentRecord(TimestampMixin, Base):
    __tablename__ = "experiments"
    __table_args__ = (CheckConstraint("status IN ('draft','ready','active','needs_revision','closed')", name="ck_experiment_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "exp_" + str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"), nullable=False)
    candidate_experiment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    head_revision_id: Mapped[str | None] = mapped_column(String(36))
    repairs_used: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    repairs_limit: Mapped[int] = mapped_column(BIGINT, nullable=False, default=5)


class SpecRevisionRecord(Base):
    __tablename__ = "spec_revisions"
    __table_args__ = (UniqueConstraint("experiment_id", "revision_no", name="uq_spec_revision_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "spr_" + str(uuid.uuid4()))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(BIGINT, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    comparison_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class ConfirmationRecord(Base):
    __tablename__ = "confirmations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "cnf_" + str(uuid.uuid4()))
    revision_id: Mapped[str] = mapped_column(ForeignKey("spec_revisions.id"), nullable=False, unique=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identity_users.id"), nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_issue_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class RunRecord(TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (CheckConstraint("status IN ('queued','created','running','evaluating','cancel_requested','completed','failed','cancelled','awaiting_revision')", name="ck_run_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "run_" + str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("spec_revisions.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="local-cpu")
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_hash: Mapped[str | None] = mapped_column(String(64))
    report_hash: Mapped[str | None] = mapped_column(String(64))
    active_report_build_id: Mapped[str | None] = mapped_column(String(36))
    # Execution truth pinned at creation time: the gating environment check and
    # the explicitly confirmed execution target. Nullable only for rows that
    # predate this field; new Runs always pin both.
    environment_check_id: Mapped[str | None] = mapped_column(String(36))
    target_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verdict: Mapped[str | None] = mapped_column(String(64))
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AttemptRecord(TimestampMixin, Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_index", name="uq_attempts_run_index"),
        UniqueConstraint("id", "run_id", name="uq_attempts_id_run"),
        CheckConstraint("source IN ('template','model','repair')", name="ck_attempt_source"),
        CheckConstraint("status IN ('candidate','proposed','validated','rejected')", name="ck_attempt_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "att_" + str(uuid.uuid4()))
    # Two linkage modes, mutually exclusive by convention:
    # - run-scoped attempts (template/repair): run_id set, experiment_id NULL;
    # - experiment-level model attempts (paper→code codegen): experiment_id
    #   set, run_id NULL — they are created before any Run exists and are
    #   bound to a Run later by the run-creation gate.
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), nullable=True, index=True)
    attempt_index: Mapped[int] = mapped_column(BIGINT, nullable=False)
    repair_ordinal: Mapped[int | None] = mapped_column(BIGINT)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    base_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str | None] = mapped_column(String(64))
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    code_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


# Experiment-level attempts are unique per (experiment_id, attempt_index) on
# rows not yet bound to a run; run-scoped rows keep the (run_id,
# attempt_index) unique constraint declared on the table, so neither
# constraint can collide with the other. Kept in create_all/migration
# parity with the audit-log DDL pattern above.
_ATTEMPT_EXPERIMENT_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempts_experiment_index "
    "ON attempts (experiment_id, attempt_index) "
    "WHERE experiment_id IS NOT NULL AND run_id IS NULL"
)
for _dialect in ("sqlite", "postgresql"):
    event.listen(AttemptRecord.__table__, "after_create", DDL(
        _ATTEMPT_EXPERIMENT_INDEX_DDL).execute_if(dialect=_dialect))


class ExecutionRecord(TimestampMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_executions_job"),
        UniqueConstraint("handle", name="uq_executions_handle"),
        ForeignKeyConstraint(["attempt_id", "run_id"], ["attempts.id", "attempts.run_id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','recovered')", name="ck_execution_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "exe_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    runner_id: Mapped[str | None] = mapped_column(String(255))
    fencing_token: Mapped[int] = mapped_column(BIGINT, nullable=False)
    handle: Mapped[str | None] = mapped_column(String(255), unique=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class RepairProposalRecord(Base):
    __tablename__ = "repair_proposals"
    __table_args__ = (
        UniqueConstraint("attempt_id", "repair_ordinal", name="uq_repair_proposals_attempt_ordinal"),
        ForeignKeyConstraint(["attempt_id", "run_id"], ["attempts.id", "attempts.run_id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('proposed','accepted','rejected','needs_revision')", name="ck_repair_proposal_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "rpr_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    repair_ordinal: Mapped[int] = mapped_column(BIGINT, nullable=False)
    base_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "rnev_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BIGINT, nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class ExportRecord(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "exp_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    requested_formats_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    report_hash: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[str | None] = mapped_column(String(36))
    artifact_id: Mapped[str | None] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64))
    bytes: Mapped[int | None] = mapped_column(BIGINT)
    media_type: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class ReportBuildRecord(TimestampMixin, Base):
    __tablename__ = "report_builds"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_report_build_operation"),
        UniqueConstraint("run_id", "generation", name="uq_report_build_generation"),
        CheckConstraint("status IN ('pending','generating','ready','failed')", name="ck_report_build_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "rpb_" + uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), nullable=False)
    generation: Mapped[int] = mapped_column(BIGINT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ExportItemRecord(Base):
    __tablename__ = "export_items"
    __table_args__ = (UniqueConstraint("export_id", "format", name="uq_export_item_format"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "exi_" + uuid.uuid4().hex)
    export_id: Mapped[str] = mapped_column(ForeignKey("exports.id", ondelete="CASCADE"), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64))
    bytes: Mapped[int | None] = mapped_column(BIGINT)
    media_type: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str | None] = mapped_column(String(255))
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reuse_key: Mapped[str | None] = mapped_column(String(64))


class ExportArtifactRecord(Base):
    __tablename__ = "export_artifacts"
    reuse_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)


class DemoWriteReceiptRecord(Base):
    __tablename__ = "demo_write_receipts"
    __table_args__ = (UniqueConstraint("actor_id", "key", name="uq_demo_write_receipt_actor_key"),)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    target_path: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))


class EvaluationRecord(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "eval_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(128), nullable=False)


class ReportRecord(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "rep_" + str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
