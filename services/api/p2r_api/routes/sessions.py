from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sqlalchemy import select

from p2r_contracts.sessions import CellCreate, CheckpointCreate, RestoreRequest
from p2r_orchestration.service import OrchestrationService, NotFoundError
from p2r_persistence.repositories import IdempotencyRepository

from ..dependencies import actor_id, get_service

router = APIRouter()


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trust_class: str = "untrusted"
    metadata: dict[str, Any] = Field(default_factory=dict)


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def session_json(row: Any) -> dict[str, Any]:
    return {"id": row.id, "user_id": row.user_id, "status": row.status, "metadata": row.metadata_json, "created_at": row.created_at.isoformat()}


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session(project_id: str, body: SessionBody, request: Request, service: OrchestrationService = Depends(get_service)):
    user = actor_id(request)
    row = service.create_session(user, body.metadata, trust_class=body.trust_class, project_id=project_id)
    return envelope(request, session_json(row))


@router.get("/projects/{project_id}/sessions")
def list_project_sessions(project_id: str, request: Request, service: OrchestrationService = Depends(get_service)):
    rows = service.list_sessions(actor_id(request))
    rows = [row for row in rows if (row.metadata_json or {}).get("project_id") == project_id]
    return envelope(request, [session_json(row) for row in rows])


@router.get("/sessions")
def list_sessions(request: Request, service: OrchestrationService = Depends(get_service)):
    return envelope(request, [session_json(row) for row in service.list_sessions(actor_id(request))])


def owned(service: OrchestrationService, request: Request, session_id: str):
    try:
        row = service.get_session(session_id)
    except NotFoundError:
        raise
    if row.user_id != actor_id(request):
        raise NotFoundError("session not found")
    return row


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request, service: OrchestrationService = Depends(get_service)):
    return envelope(request, session_json(owned(service, request, session_id)))


@router.post("/sessions/{session_id}/cells")
async def submit_cell(session_id: str, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key"), service: OrchestrationService = Depends(get_service)):
    if not idempotency_key:
        from p2r_orchestration.service import ValidationServiceError
        raise ValidationServiceError("Idempotency-Key is required")
    owned(service, request, session_id)
    raw = await request.body()
    try:
        payload = CellCreate.model_validate_json(raw)
    except ValidationError as exc:
        from p2r_orchestration.service import ValidationServiceError
        field_errors = []
        for error in exc.errors():
            loc = [str(item) for item in error.get("loc", ())]
            field_errors.append({"path": "/" + "/".join(loc), "code": str(error.get("type", "validation_error")), "message": str(error.get("msg", "invalid value"))})
        raise ValidationServiceError("request validation failed", details={"field_errors": field_errors or [{"path": "$", "code": "json_invalid", "message": "invalid JSON body"}]}) from exc
    result = service.submit_cell(session_id, actor_id(request), payload, idempotency_key, raw)
    return envelope(request, result.model_dump(mode="json"))


@router.get("/sessions/{session_id}/actions")
def actions(session_id: str, request: Request, after: int = -1, limit: int = 50, service: OrchestrationService = Depends(get_service)):
    owned(service, request, session_id)
    return envelope(request, [item.model_dump(mode="json") for item in service.list_actions(session_id, after=after, limit=limit)])


def _checkpoint_body(body: CheckpointCreate) -> bytes:
    return json.dumps(body.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":")).encode()


def _checkpoint_replay(service: OrchestrationService, session_id: str, actor: str, key: str, body: CheckpointCreate):
    from p2r_orchestration.service import IdempotencyConflictError, IdempotencyInProgressError

    scope = f"checkpoint:{session_id}:{actor}"
    claim = IdempotencyRepository(service.db).claim(scope=scope, key=key, body=_checkpoint_body(body))
    if claim.conflict:
        raise IdempotencyConflictError("idempotency key was used with a different request body")
    if claim.record.response_json is not None:
        return claim.record.response_json
    if not claim.claimed:
        raise IdempotencyInProgressError("idempotency key is already in progress")

    result = service.create_checkpoint(session_id, body)
    response = result.model_dump(mode="json")
    claim.record.response_json = response
    service.db.commit()
    return response


@router.post("/sessions/{session_id}/checkpoints")
def checkpoint(
    session_id: str,
    body: CheckpointCreate,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    service: OrchestrationService = Depends(get_service),
):
    owned(service, request, session_id)
    if not idempotency_key:
        from p2r_orchestration.service import ValidationServiceError
        raise ValidationServiceError("Idempotency-Key is required")
    result = _checkpoint_replay(service, session_id, actor_id(request), idempotency_key, body)
    return envelope(request, result)


@router.post("/sessions/{session_id}/restore")
def restore(
    session_id: str,
    body: RestoreRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    service: OrchestrationService = Depends(get_service),
):
    owned(service, request, session_id)
    if not idempotency_key:
        from p2r_orchestration.service import ValidationServiceError
        raise ValidationServiceError("Idempotency-Key is required")

    from p2r_orchestration.service import IdempotencyConflictError, IdempotencyInProgressError

    scope = f"restore:{session_id}:{actor_id(request)}"
    claim = IdempotencyRepository(service.db).claim(
        scope=scope,
        key=idempotency_key,
        body=json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
    )
    if claim.conflict:
        raise IdempotencyConflictError("idempotency key was used with a different request body")
    if claim.record.response_json is not None:
        result = claim.record.response_json
    else:
        if not claim.claimed:
            raise IdempotencyInProgressError("idempotency key is already in progress")
        result = service.restore(session_id, body.checkpoint_id).model_dump(mode="json")
        claim.record.response_json = result
        service.db.commit()
    return envelope(request, result)


@router.get("/sessions/{session_id}/checkpoints")
def checkpoints(session_id: str, request: Request, limit: int = 50, service: OrchestrationService = Depends(get_service)):
    owned(service, request, session_id)
    return envelope(request, [item.model_dump(mode="json") for item in service.list_checkpoints(session_id, limit=limit)])


@router.get("/sessions/{session_id}/events")
def events(session_id: str, request: Request, after: int = -1, limit: int = 50, last_event_id: str | None = Header(None, alias="Last-Event-ID"), service: OrchestrationService = Depends(get_service)):
    owned(service, request, session_id)
    if after == -1 and last_event_id is not None:
        try:
            after = int(last_event_id)
        except ValueError:
            after = -1
    rows = service.get_events(session_id, after=after, limit=limit)
    def stream():
        for item in rows:
            yield f"id: {item.sequence_no}\nevent: {item.type}\ndata: {json.dumps(item.model_dump(mode='json'), separators=(',', ':'))}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
