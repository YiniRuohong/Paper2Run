from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    RecoveryRead,
    RecoveryStatus,
    SessionStatus,
)
from p2r_persistence.models import (
    Action,
    Artifact,
    CellExecution,
    Checkpoint,
    Environment,
    Event,
    IdempotencyKey,
    SessionRecord,
)
from p2r_persistence.repositories import ActionRepository, EventRepository, IdempotencyRepository

from .environment import environment_snapshot


MAX_OUTPUT = 256 * 1024
MAX_RESULT = 16 * 1024
MAX_PAGE = 200


class ServiceError(Exception):
    status = 400
    code = "SERVICE_ERROR"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(ServiceError):
    status = 404
    code = "NOT_FOUND"


class ConflictError(ServiceError):
    status = 409
    code = "CONFLICT"


class IdempotencyConflictError(ConflictError):
    code = "CONFLICT"


class IdempotencyInProgressError(ConflictError):
    code = "IDEMPOTENCY_IN_PROGRESS"


class PermissionDeniedError(ServiceError):
    status = 403
    code = "PERMISSION_DENIED"


class InvalidStateError(ServiceError):
    status = 409
    code = "INVALID_STATE"


class ValidationServiceError(ServiceError):
    status = 422
    code = "VALIDATION_ERROR"


class KernelServiceError(ServiceError):
    status = 500
    code = "KERNEL_ERROR"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_body(cell: CellCreate) -> bytes:
    # Pydantic's JSON representation is the public request canonical form.
    return cell.model_dump_json().encode("utf-8")


def _canonical_request_body(raw_body: bytes | str) -> bytes:
    try:
        value = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationServiceError(
            "raw_body must be valid JSON",
            details={"field_errors": [{"path": "$", "code": "json_invalid", "message": "invalid JSON body"}]},
        ) from exc
    return _canonical_json(value).encode("utf-8")


def _bounded(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def _page(value: int | None, default: int = 50) -> int:
    if value is None:
        return default
    if value < 1 or value > MAX_PAGE:
        raise ValidationServiceError("limit must be between 1 and 200")
    return value


class OrchestrationService:
    """Application service for session, execution, and append-only history operations."""

    def __init__(self, db: Session, supervisor: Any = None, artifact_store: Any = None):
        self.db = db
        self.supervisor = supervisor
        self.artifact_store = artifact_store

    def create_session(
        self,
        user_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        trust_class: str | None = None,
        project_id: str | None = None,
    ) -> SessionRecord:
        if not user_id:
            raise ValidationServiceError("user_id is required")
        data = dict(metadata or {})
        if trust_class is not None:
            data["trust_class"] = str(trust_class)
        if project_id is not None:
            data["project_id"] = project_id
        record = SessionRecord(user_id=user_id, status=SessionStatus.ACTIVE.value, metadata_json=data)
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def get_session(self, session_id: str) -> SessionRecord:
        record = self.db.get(SessionRecord, session_id)
        if record is None:
            raise NotFoundError("session not found")
        return record

    def list_sessions(self, user_id: str | None = None, *, limit: int | None = None, after: str | None = None) -> list[SessionRecord]:
        query = select(SessionRecord).order_by(SessionRecord.id).limit(_page(limit))
        if user_id is not None:
            query = query.where(SessionRecord.user_id == user_id)
        if after:
            query = query.where(SessionRecord.id > after)
        return list(self.db.scalars(query))

    def submit_cell(
        self,
        session_id: str,
        actor_id: str,
        cell: CellCreate,
        idempotency_key: str,
        raw_body: bytes | str,
    ) -> CellRead:
        session = self.get_session(session_id)
        if session.status != SessionStatus.ACTIVE.value:
            raise InvalidStateError("session is not active")
        if cell.session_id != session_id:
            raise ValidationServiceError("cell session_id does not match path")
        metadata = session.metadata_json or {}
        role = str(metadata.get("role", metadata.get("actor_role", ""))).lower()
        if role in {"viewer", "read_only", "readonly"} or metadata.get("read_only") is True:
            raise PermissionDeniedError("read-only actor cannot submit cells")
        allowed = metadata.get("capabilities")
        requested = [str(cap) for cap in cell.requested_capabilities]
        if isinstance(allowed, (list, tuple, set)) and any(cap not in {str(v) for v in allowed} for cap in requested):
            raise PermissionDeniedError("requested capability is not allowed")
        if not idempotency_key:
            raise ValidationServiceError("idempotency_key is required")

        canonical_body = _canonical_body(cell)
        try:
            claim = IdempotencyRepository(self.db).claim(scope=f"cell:{session_id}:{actor_id}", key=idempotency_key, body=canonical_body)
        except Exception:
            self.db.rollback()
            raise
        if claim.conflict:
            raise IdempotencyConflictError("idempotency key was used with a different request body")
        if claim.record.response_json is not None:
            return CellRead.model_validate(claim.record.response_json)
        if not getattr(claim, "claimed", False):
            raise IdempotencyInProgressError("idempotency key is already in progress")
        try:
            supplied_cell = CellCreate.model_validate_json(raw_body)
        except Exception as exc:
            raise ValidationServiceError(
                "raw_body must be valid cell JSON",
                details={"field_errors": [{"path": "$", "code": "cell_invalid", "message": "body does not match cell schema"}]},
            ) from exc
        if supplied_cell != cell:
            raise ValidationServiceError(
                "raw_body does not match cell payload",
                details={"field_errors": [{"path": "$", "code": "body_mismatch", "message": "body does not match validated cell payload"}]},
            )

        cell_id = str(uuid.uuid4())
        source_hash = _hash(cell.source.encode("utf-8"))
        action_seq = self._next_action_sequence(session_id)
        action_payload = {
            "actor_id": actor_id,
            "cell_id": cell_id,
            "source_hash": source_hash,
            "requested_capabilities": requested,
            "base_checkpoint_id": cell.base_checkpoint_id,
            "permission_decision": PermissionDecision.ALLOW.value,
        }
        action_payload["payload_hash"] = _hash(_canonical_json(action_payload).encode("utf-8"))
        action = ActionRepository(self.db).append(
            session_id=session_id,
            sequence_no=action_seq,
            kind="cell.submit",
            payload=action_payload,
        )
        execution = CellExecution(
            id=cell_id,
            session_id=session_id,
            cell_id=cell_id,
            status=CellStatus.QUEUED.value,
            input_text=cell.source,
            output_json={},
        )
        self.db.add(execution)
        self.db.flush()
        execution.status = CellStatus.RUNNING.value
        self.db.flush()

        env_spec = environment_snapshot()
        environment = Environment(session_id=session_id, name="runtime", spec=env_spec)
        self.db.add(environment)
        self.db.flush()

        try:
            result = self._execute(cell.source)
        except Exception as exc:
            execution.status = CellStatus.FAILED.value
            execution.output_json = {"stdout": "", "stderr": "", "result_summary": "", "namespace_hash": "0" * 64, "error_code": "KERNEL_ERROR", "artifact_ids": []}
            EventRepository(self.db).append(
                session_id=session_id,
                sequence_no=self._next_event_sequence(session_id),
                kind="cell.completed",
                payload={"action_sequence": action_seq, "cell_id": cell_id, "status": CellStatus.FAILED.value, "error_code": "KERNEL_ERROR", "artifact_ids": []},
            )
            self.db.commit()
            raise KernelServiceError("kernel execution failed") from exc
        stdout = _bounded(getattr(result, "stdout", ""), MAX_OUTPUT)
        stderr = _bounded(getattr(result, "stderr", ""), MAX_OUTPUT)
        summary = _bounded(getattr(result, "result_summary", ""), MAX_RESULT)
        namespace_hash = getattr(result, "namespace_hash", "0" * 64)
        if not isinstance(namespace_hash, str) or len(namespace_hash) != 64:
            namespace_hash = "0" * 64
        error_code = getattr(result, "error_code", None)
        status = CellStatus.TIMED_OUT.value if error_code == "CELL_TIMEOUT" else (
            CellStatus.FAILED.value if error_code else CellStatus.SUCCEEDED.value
        )
        try:
            artifact_ids = self._store_outputs(session_id, stdout, stderr, action_id=cell_id, trust_class=str((metadata.get("trust_class") or "untrusted")))
        except Exception as exc:
            execution.status = CellStatus.FAILED.value
            execution.output_json = {"stdout": stdout, "stderr": stderr, "result_summary": summary, "namespace_hash": namespace_hash, "error_code": "ARTIFACT_ERROR", "artifact_ids": []}
            EventRepository(self.db).append(
                session_id=session_id,
                sequence_no=self._next_event_sequence(session_id),
                kind="cell.completed",
                payload={"action_sequence": action_seq, "cell_id": cell_id, "status": CellStatus.FAILED.value, "error_code": "ARTIFACT_ERROR", "artifact_ids": []},
            )
            self.db.commit()
            raise ServiceError("artifact storage failed") from exc
        execution.status = status
        execution.output_json = {
            "stdout": stdout,
            "stderr": stderr,
            "result_summary": summary,
            "namespace_hash": namespace_hash,
            "error_code": error_code,
            "artifact_ids": artifact_ids,
        }
        # Actions are append-only. Execution details belong to the immutable
        # CellExecution and completion Event rows, so never mutate this row.
        completion_seq = self._next_event_sequence(session_id)
        event_payload = {
            "action_sequence": action_seq,
            "cell_id": cell_id,
            "status": status,
            "error_code": error_code,
            "artifact_ids": artifact_ids,
        }
        EventRepository(self.db).append(
            session_id=session_id,
            sequence_no=completion_seq,
            kind="cell.completed",
            payload=event_payload,
        )
        response = CellRead(
            id=cell_id,
            session_id=session_id,
            status=status,
            source_hash=source_hash,
            stdout=stdout,
            stderr=stderr,
            result_summary=summary,
            namespace_hash=namespace_hash,
            error_code=error_code,
            artifact_ids=artifact_ids,
            environment_id=environment.id,
            created_at=execution.created_at or _now(),
        )
        claim.record.response_json = response.model_dump(mode="json")
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError("append-only sequence conflict") from exc
        return response

    def list_actions(self, session_id: str, *, after: int = -1, limit: int | None = None) -> list[ActionRead]:
        self.get_session(session_id)
        if after < -1:
            raise ValidationServiceError("after must be non-negative")
        rows = list(self.db.scalars(select(Action).where(Action.session_id == session_id, Action.sequence_no > after).order_by(Action.sequence_no).limit(_page(limit))))
        return [self._action_read(row) for row in rows]

    def create_checkpoint(self, session_id: str, checkpoint: CheckpointCreate) -> CheckpointRead:
        self.get_session(session_id)
        max_action = self.db.scalar(select(func.max(Action.sequence_no)).where(Action.session_id == session_id))
        max_cursor = (int(max_action) + 1) if max_action is not None else 0
        if checkpoint.action_cursor > max_cursor:
            raise ValidationServiceError("checkpoint action_cursor exceeds session action cursor")
        manifest = checkpoint.manifest or {
            "action_cursor": checkpoint.action_cursor,
            "input_artifact_ids": checkpoint.input_artifact_ids,
            "environment_id": checkpoint.environment_id,
        }
        from p2r_contracts.common import sha256_hex
        if checkpoint.manifest is not None and sha256_hex(manifest) != checkpoint.manifest_hash.lower():
            raise ValidationServiceError("manifest_hash does not match manifest")
        if checkpoint.environment_id:
            environment = self.db.get(Environment, checkpoint.environment_id)
            if environment is None or environment.session_id != session_id:
                raise ValidationServiceError("checkpoint environment does not belong to session")
        if checkpoint.input_artifact_ids:
            count = self.db.scalar(select(func.count(Artifact.id)).where(Artifact.session_id == session_id, Artifact.id.in_(checkpoint.input_artifact_ids)))
            if count != len(set(checkpoint.input_artifact_ids)):
                raise ValidationServiceError("checkpoint input artifact does not belong to session")
        manifest_artifact_id = None
        if self.artifact_store is not None:
            address = self.artifact_store.put_bytes(_canonical_json(manifest).encode("utf-8"))
            # The manifest artifact is the content-addressed source of truth.
            checkpoint_manifest_hash = address.sha256
            artifact = Artifact(session_id=session_id, storage_key=str(address.path), sha256=address.sha256, bytes=address.bytes, media_type="application/json", metadata_json={"kind": "checkpoint_manifest"})
            self.db.add(artifact)
            self.db.flush()
            manifest_artifact_id = artifact.id
        state = checkpoint.model_copy(update={"manifest": manifest, "manifest_hash": locals().get("checkpoint_manifest_hash", checkpoint.manifest_hash)}).model_dump(mode="json")
        row = Checkpoint(session_id=session_id, name=checkpoint.name, state=state, manifest_artifact_id=manifest_artifact_id)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return CheckpointRead(**state, id=row.id, session_id=session_id, status="created", reasons=[], manifest_artifact_id=manifest_artifact_id, created_at=row.created_at)

    def classify_recovery(self, session_id: str, checkpoint_id: str) -> RecoveryRead:
        self.get_session(session_id)
        row = self.db.get(Checkpoint, checkpoint_id)
        if row is None or row.session_id != session_id:
            raise NotFoundError("checkpoint not found")
        reasons: list[str] = []
        state = row.state or {}
        manifest = state.get("manifest")
        if not isinstance(manifest, dict):
            reasons.append("manifest_missing")
        else:
            from p2r_contracts.common import sha256_hex
            if sha256_hex(manifest) != str(state.get("manifest_hash", "")).lower():
                reasons.append("manifest_hash_mismatch")
            try:
                if int(manifest.get("action_cursor", -1)) != int(state.get("action_cursor", -2)):
                    reasons.append("manifest_action_cursor_mismatch")
                if manifest.get("environment_id") != state.get("environment_id"):
                    reasons.append("manifest_environment_mismatch")
                if sorted(manifest.get("input_artifact_ids", [])) != sorted(state.get("input_artifact_ids", [])):
                    reasons.append("manifest_input_artifacts_mismatch")
            except (TypeError, ValueError):
                reasons.append("manifest_invalid")
        environment_id = state.get("environment_id")
        if not isinstance(environment_id, str):
            reasons.append("environment_missing")
        else:
            environment = self.db.get(Environment, environment_id)
            if environment is None or environment.session_id != session_id:
                reasons.append("environment_missing")
        input_artifact_ids = state.get("input_artifact_ids", [])
        if not isinstance(input_artifact_ids, list) or any(not isinstance(item, str) for item in input_artifact_ids):
            reasons.append("input_artifacts_invalid")
        elif input_artifact_ids:
            artifacts = list(self.db.scalars(select(Artifact).where(Artifact.session_id == session_id, Artifact.id.in_(input_artifact_ids))))
            if len(artifacts) != len(set(input_artifact_ids)):
                reasons.append("input_artifacts_missing")
            elif self.artifact_store is not None:
                for artifact in artifacts:
                    try:
                        content = self.artifact_store.open_bytes(artifact.sha256)
                    except Exception:
                        reasons.append("input_artifact_hash_mismatch")
                        break
                    if _hash(content) != artifact.sha256:
                        reasons.append("input_artifact_hash_mismatch")
                        break
        cursor = state.get("action_cursor")
        max_action = self.db.scalar(select(func.max(Action.sequence_no)).where(Action.session_id == session_id))
        max_cursor = int(max_action) + 1 if max_action is not None else 0
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0 or cursor > max_cursor:
            reasons.append("action_cursor_invalid")
        elif cursor:
            action_count = int(self.db.scalar(
                select(func.count(Action.id)).where(Action.session_id == session_id, Action.sequence_no < cursor)
            ) or 0)
            if action_count != cursor:
                reasons.append("action_cursor_gap")
        if row.manifest_artifact_id is None:
            reasons.append("manifest_artifact_missing")
        else:
            artifact = self.db.get(Artifact, row.manifest_artifact_id)
            if artifact is None:
                reasons.append("manifest_artifact_missing")
            elif self.artifact_store is not None:
                try:
                    content = self.artifact_store.open_bytes(artifact.sha256)
                except Exception:
                    reasons.append("manifest_artifact_hash_mismatch")
                else:
                    if _hash(content) != artifact.sha256 or _hash(content) != str(state.get("manifest_hash", "")).lower():
                        reasons.append("manifest_artifact_hash_mismatch")
        if self.supervisor is None or not callable(getattr(self.supervisor, "restart", None)):
            reasons.append("supervisor_restart_unavailable")
        status = RecoveryStatus.RECOVERED if not reasons else RecoveryStatus.PARTIAL
        if any(
            "missing" in item or "mismatch" in item or item.endswith("invalid") or "gap" in item
            for item in reasons
        ):
            status = RecoveryStatus.FAILED
        return RecoveryRead(status=status, reasons=reasons, checkpoint_id=checkpoint_id)

    def restore(self, session_id: str, checkpoint_id: str) -> RecoveryRead:
        result = self.classify_recovery(session_id, checkpoint_id)
        if result.status == RecoveryStatus.PARTIAL and "supervisor_restart_unavailable" in result.reasons:
            return result
        row = self.db.get(Checkpoint, checkpoint_id)
        if result.status == RecoveryStatus.RECOVERED and callable(getattr(self.supervisor, "restart", None)):
            self.supervisor.restart(row.state.get("manifest", {}))
        return result

    def list_checkpoints(self, session_id: str, *, limit: int | None = None) -> list[CheckpointRead]:
        self.get_session(session_id)
        rows = list(self.db.scalars(select(Checkpoint).where(Checkpoint.session_id == session_id).order_by(Checkpoint.created_at, Checkpoint.id).limit(_page(limit))))
        return [CheckpointRead(**row.state, id=row.id, session_id=row.session_id, status="created", reasons=[], manifest_artifact_id=row.manifest_artifact_id, created_at=row.created_at) for row in rows]

    def get_events(self, session_id: str, *, after: int = -1, limit: int | None = None) -> list[EventRead]:
        self.get_session(session_id)
        if after < -1:
            raise ValidationServiceError("after must be non-negative")
        rows = list(self.db.scalars(select(Event).where(Event.session_id == session_id, Event.sequence_no > after).order_by(Event.sequence_no).limit(_page(limit))))
        return [EventRead(sequence_no=row.sequence_no, type=row.event_type, payload=row.payload, created_at=row.created_at) for row in rows]

    def _next_action_sequence(self, session_id: str) -> int:
        current = self.db.scalar(select(func.max(Action.sequence_no)).where(Action.session_id == session_id))
        return (int(current) if current is not None else -1) + 1

    def _next_event_sequence(self, session_id: str) -> int:
        current = self.db.scalar(select(func.max(Event.sequence_no)).where(Event.session_id == session_id))
        return (int(current) if current is not None else -1) + 1

    def _execute(self, source: str) -> Any:
        if self.supervisor is None:
            raise ValidationServiceError("kernel supervisor is not configured")
        try:
            from p2r_kernel.protocol import ExecuteCellRequest
            request = ExecuteCellRequest(source=source)
        except ImportError:
            request = type("ExecuteCellRequest", (), {"source": source})()
        return self.supervisor.execute(request)

    def _store_outputs(self, session_id: str, stdout: str, stderr: str, *, action_id: str, trust_class: str) -> list[str]:
        if self.artifact_store is None:
            return []
        ids: list[str] = []
        for label, text in (("stdout", stdout), ("stderr", stderr)):
            if not text:
                continue
            address = self.artifact_store.put_bytes(text.encode("utf-8"))
            artifact = Artifact(
                session_id=session_id,
                storage_key=str(address.path),
                sha256=address.sha256,
                bytes=address.bytes,
                media_type="text/plain; charset=utf-8",
                metadata_json={
                    "stream": label,
                    "producer_action": action_id,
                    "trust_class": trust_class,
                    "provenance": {"source": "cell_execution", "session_id": session_id},
                },
            )
            self.db.add(artifact)
            self.db.flush()
            ids.append(artifact.id)
        return ids

    @staticmethod
    def _action_read(row: Action) -> ActionRead:
        payload = row.payload or {}
        status = payload.get("status")
        if status not in {item.value for item in ActionStatus}:
            status = ActionStatus.PENDING.value
        decision = payload.get("permission_decision")
        if decision not in {item.value for item in PermissionDecision}:
            decision = PermissionDecision.PROMPT.value
        canonical = {key: value for key, value in payload.items() if key != "payload_hash"}
        payload_hash = payload.get("payload_hash") or _hash(_canonical_json(canonical).encode("utf-8"))
        actor_id = payload.get("actor_id")
        if not actor_id:
            actor_id = "unknown"
        return ActionRead(
            sequence_no=row.sequence_no,
            kind=row.action_type,
            status=status,
            payload_hash=payload_hash,
            actor_id=str(payload.get("actor_id", "system")),
            cell_id=payload.get("cell_id"),
            artifact_ids=payload.get("artifact_ids", []),
            permission_decision=decision,
            created_at=row.created_at,
        )
