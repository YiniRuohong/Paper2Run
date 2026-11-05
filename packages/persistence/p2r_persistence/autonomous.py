"""Independent persistence for autonomous tasks; never stores runtime credentials."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DDL, ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import TimestampMixin, UTCDateTime, utcnow


class AutonomousConfigRecord(TimestampMixin, Base):
    __tablename__ = "autonomous_configs"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_autonomous_config_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class AutonomousTaskRecord(TimestampMixin, Base):
    __tablename__ = "autonomous_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="queued")
    objective: Mapped[str] = mapped_column(String(8192))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Reference values are control-plane metadata, never model-authored evidence.
    reference_values_json: Mapped[dict | None] = mapped_column(JSON)
    reference_scope: Mapped[str | None] = mapped_column(String(8192))
    reference_sources_confirmed: Mapped[bool] = mapped_column(default=False)
    skills_json: Mapped[list] = mapped_column(JSON, default=list)
    protocol_json: Mapped[dict | None] = mapped_column(JSON)
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    jobs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts_json: Mapped[list] = mapped_column(JSON, default=list)
    report_json: Mapped[dict | None] = mapped_column(JSON)
    error_json: Mapped[dict | None] = mapped_column(JSON)
    pending_decision_json: Mapped[dict | None] = mapped_column(JSON)
    session_file: Mapped[str | None] = mapped_column(String(2048))
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)


class AutonomousEventRecord(Base):
    __tablename__ = "autonomous_events"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_autonomous_event_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("autonomous_tasks.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# Event history is append-only on both create_all and migrated SQLite databases.
for operation in ("UPDATE", "DELETE"):
    event.listen(AutonomousEventRecord.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS p2r_autonomous_events_{operation.lower()} "
        f"BEFORE {operation} ON autonomous_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
    ).execute_if(dialect="sqlite"))
