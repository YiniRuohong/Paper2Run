from __future__ import annotations

import json

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from p2r_orchestration.provider_sessions import ProviderSessionClosedError
from p2r_orchestration.service import ServiceError

from ..dependencies import actor_id
from .papers import _envelope, _error, _check_write  # reuse envelope/write/CSRF patterns

router = APIRouter()


class ProviderSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=2048)
    endpoint_alias: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    revision: str = Field(default="1", min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=4096)
    allow_local_http: bool = False
    # Optional capability declaration; unknown is the safe default.
    capabilities: dict[str, str] | None = None


def _store(request: Request):
    return request.app.state.runtime["provider_secret_store"]


@router.post("/provider-sessions", status_code=201)
async def create_provider_session(request: Request):
    """Create a runtime-only provider session. Write, so it requires Idempotency-Key."""
    runtime = request.app.state.runtime
    user_id = actor_id(request)
    with runtime["session_factory"]() as db:
        # No project scoping: provider sessions are actor-scoped runtime state.
        # Still run the shared write (CSRF/origin) check used by other writes.
        _check_write_global(request)
        key = request.headers.get("idempotency-key")
        if not key:
            _error("VALIDATION_ERROR", "Idempotency-Key is required")
        body = await request.body()
        claim = _claim(request, db, scope=f"provider-session:{user_id}", key=key, body=body)
        if claim is not None:
            return _envelope(request, claim)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            _error("VALIDATION_ERROR", "invalid JSON")
        try:
            create = ProviderSessionCreate.model_validate(payload)
        except Exception:
            _error("VALIDATION_ERROR", "invalid provider session payload")
        store = _store(request)
        if store is None:
            _error("PROVIDER_SESSION_UNAVAILABLE", "provider sessions are unavailable", status=503)
        from p2r_orchestration.providers import Capabilities

        if create.capabilities is not None:
            try:
                capabilities = Capabilities.model_validate(create.capabilities)
            except Exception:
                _error("VALIDATION_ERROR", "invalid capabilities")
        else:
            # Default declaration for OpenAI-compatible endpoints: plain text
            # and JSON-schema structured output are served by the base API;
            # streaming/tools/image stay "unknown" until probed. Explicit
            # client declarations always win over this default.
            capabilities = Capabilities(text="supported", structured_output="supported")
        try:
            session = store.put(
                base_url=create.base_url,
                endpoint_alias=create.endpoint_alias,
                model_id=create.model_id,
                api_key=create.api_key,
                display_name=create.display_name,
                revision=create.revision,
                allow_local_http=create.allow_local_http,
                capabilities=capabilities,
            )
        except Exception:
            _error("PROVIDER_ENDPOINT_REJECTED", "provider endpoint was rejected")
        summary = store.public_summary(session.id)
        _save(request, db, scope=f"provider-session:{user_id}", key=key, response=summary)
        db.commit()
        return _envelope(request, summary)


@router.get("/provider-sessions/{provider_session_id}")
def get_provider_session(provider_session_id: str, request: Request):
    _ = actor_id(request)
    store = _store(request)
    try:
        return _envelope(request, store.public_summary(provider_session_id))
    except ProviderSessionClosedError:
        _error("RESOURCE_NOT_FOUND", "resource not found", status=404)


@router.delete("/provider-sessions/{provider_session_id}", status_code=200)
async def delete_provider_session(provider_session_id: str, request: Request):
    runtime = request.app.state.runtime
    user_id = actor_id(request)
    with runtime["session_factory"]() as db:
        _check_write_global(request)
        key = request.headers.get("idempotency-key")
        if not key:
            _error("VALIDATION_ERROR", "Idempotency-Key is required")
        body = await request.body()
        claim = _claim(request, db, scope=f"provider-session-clear:{user_id}", key=key, body=body)
        if claim is not None:
            return _envelope(request, claim)
        store = _store(request)
        if store is None:
            _error("PROVIDER_SESSION_UNAVAILABLE", "provider sessions are unavailable", status=503)
        store.clear(provider_session_id)
        summary = {"provider_session_id": provider_session_id, "secret_configured": False}
        _save(request, db, scope=f"provider-session-clear:{user_id}", key=key, response=summary)
        db.commit()
        return _envelope(request, summary)


# --- helpers mirroring papers.py's idempotency pattern ----------------------


def _check_write_global(request: Request):
    if request.app.state.runtime.get("development_auth"):
        return
    from ..auth import csrf_token, validate_csrf, validate_origin

    allowed = request.app.state.runtime.get("allowed_origin")
    if not allowed:
        _error("CSRF_FAILED", "origin rejected", status=403)
    validate_origin(request.headers.get("origin"), allowed)
    secret = request.cookies.get("p2r_session")
    if not secret:
        _error("SESSION_REQUIRED", "session required", status=401)
    validate_csrf(request.headers.get("x-csrf-token"), csrf_token(secret))


def _claim(request, db, *, scope: str, key: str, body: bytes):
    from p2r_persistence.repositories import IdempotencyRepository

    claim = IdempotencyRepository(db).claim(scope=scope, key=key, body=body)
    if claim.conflict:
        _error("IDEMPOTENCY_CONFLICT",
               "idempotency key was used with a different request body", status=409)
    if claim.record.response_json is not None:
        return claim.record.response_json
    if not claim.claimed:
        _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", status=409)
    return None


def _save(request, db, *, scope: str, key: str, response):
    from p2r_persistence.models import IdempotencyKey
    from sqlalchemy import select

    row = db.scalar(select(IdempotencyKey).where(
        IdempotencyKey.scope == scope, IdempotencyKey.key == key))
    if row is not None:
        row.response_json = response
