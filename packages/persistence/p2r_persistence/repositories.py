from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Action, Event, IdempotencyKey
from .job_repository import JobRepository


class AppendOnlyRepository:
    """Boundary for immutable action/event log writes."""
    model: type[Action | Event]

    def __init__(self, session: Session):
        self.session = session

    def append(self, *, session_id: str, sequence_no: int, kind: str, payload: dict[str, Any]):
        row = self.model(session_id=session_id, sequence_no=sequence_no, **{self.kind_field: kind}, payload=payload)
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            # Only the declared composite unique constraint is a duplicate.
            # A pre-existing row is not sufficient evidence: another
            # constraint (most notably the session FK) may have failed.
            constraint = getattr(getattr(exc, "orig", None), "diag", None)
            constraint_name = getattr(constraint, "constraint_name", None)
            message = str(getattr(exc, "orig", exc)).lower()
            expected = f"uq_{self.model.__tablename__}_session_sequence"
            sqlite_columns = f"{self.model.__tablename__}.session_id, {self.model.__tablename__}.sequence_no"
            is_duplicate = constraint_name == expected or (
                constraint_name is None
                and (expected in message or sqlite_columns in message)
            )
            if not is_duplicate:
                raise
            raise ValueError(f"duplicate sequence {session_id}:{sequence_no}") from None
        return row


class ActionRepository(AppendOnlyRepository):
    model = Action
    kind_field = "action_type"


class EventRepository(AppendOnlyRepository):
    model = Event
    kind_field = "event_type"


@dataclass(frozen=True)
class IdempotencyResult:
    record: IdempotencyKey
    conflict: bool = False
    claimed: bool = False


class IdempotencyRepository:
    def __init__(self, session: Session):
        self.session = session

    def claim(self, *, scope: str, key: str, body: bytes | str) -> IdempotencyResult:
        raw = body.encode() if isinstance(body, str) else body
        digest = sha256(raw).hexdigest()
        existing = self.session.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key))
        if existing:
            return IdempotencyResult(existing, conflict=existing.body_hash != digest, claimed=False)
        record = IdempotencyKey(scope=scope, key=key, body_hash=digest)
        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key))
            if existing is None:
                raise ValueError("idempotency claim could not be resolved") from None
            return IdempotencyResult(existing, conflict=existing.body_hash != digest, claimed=False)
        return IdempotencyResult(record, claimed=True)
