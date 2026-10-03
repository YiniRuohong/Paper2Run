from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import delete, func, select

from p2r_artifacts.store import ArtifactIntegrityError, ArtifactNotFoundError, LocalArtifactStore
from p2r_persistence.models import (
    AttemptRecord,
    ExperimentRecord,
    ExtractionRecord,
    IdempotencyKey,
    PaperRecord,
    SpecRevisionRecord,
)
from p2r_persistence.repositories import IdempotencyRepository
from p2r_orchestration.service import ServiceError
from ..auth import require_project_access
from ..dependencies import actor_id
from .experiments import write_check
from .papers import _provider_code

router = APIRouter()

# CodegenRequest: the only body fields the codegen endpoint accepts. Unknown
# keys are refused so an api_key (or any credential) can never ride along;
# provider credentials come only from the runtime secret store.
_ALLOWED_CODEGEN_KEYS = {"provider_session_id", "reason", "prompt_version"}
_CODEGEN_POLICY_VERSION = "codegen-policy-1"


def err(code: str, message: str, status: int = 422, details: dict | None = None):
    exc = ServiceError(message, details=details or {})
    exc.code, exc.status = code, status
    raise exc


def envelope(request: Request, data: Any):
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def _artifact_store(runtime):
    store = runtime.get("artifact_store")
    if store is None:
        # Same default the paper upload path uses when no store is injected.
        store = LocalArtifactStore(Path(os.getenv("P2R_ARTIFACT_ROOT", ".p2r-artifacts")))
    return store


def _resolve_codegen_service(runtime, provider_session_id: str | None):
    """Injection seam for the codegen service (kept minimal on purpose).

    1. ``runtime["codegen_service"]`` — any object exposing ``generate``;
       tests and ops inject a fake- or real-provider CodegenService here.
       The default wiring in dependencies.py deliberately does not set it.
    2. Otherwise the default wiring fallback: when
       ``runtime["extraction_service"]`` is present the app is provider
       capable, so a CodegenService is built per call around an
       ``OpenAICompatibleProvider`` resolved from the same runtime-only
       secret store — exactly how SessionExtractionService builds its
       provider (config from ``config(session_id)``, secret lazily via
       ``get(session_id)``; the secret never leaves the provider call).

    Provider/model identity always comes from the secret store, never from
    the request body. Returns (service, provider_config_id, model_id).
    """
    from p2r_orchestration.codegen_service import CodegenService
    from p2r_orchestration.providers import Capabilities, OpenAICompatibleProvider

    injected = runtime.get("codegen_service")
    has_injected = injected is not None and hasattr(injected, "generate")
    if not has_injected and runtime.get("extraction_service") is None:
        err("PROVIDER_NOT_CONFIGURED", "codegen provider is not configured", 409)
    secret_store = runtime.get("provider_secret_store")
    if secret_store is None or not isinstance(provider_session_id, str) or not provider_session_id:
        err("PROVIDER_SESSION_UNAVAILABLE", "provider session is not available", 409)
    try:
        if provider_session_id not in secret_store:
            raise LookupError(provider_session_id)
        secret_store.get(provider_session_id)
        config = secret_store.config(provider_session_id)
        provider_config_id, model_id = config.id, config.model_id
        if not model_id:
            raise ValueError("model id missing")
    except Exception:
        err("PROVIDER_SESSION_UNAVAILABLE", "provider session is not available", 409)
    if has_injected:
        return injected, provider_config_id, model_id

    def resolve_secret() -> str:
        return secret_store.get(provider_session_id)

    provider = OpenAICompatibleProvider(
        config.model_copy(update={
            "capabilities": Capabilities(text="supported", structured_output="supported"),
        }),
        secret_resolver=resolve_secret,
    )
    return CodegenService(provider, _artifact_store(runtime)), provider_config_id, model_id


def _read_artifact(runtime, sha256: str) -> bytes:
    try:
        data = _artifact_store(runtime).open_bytes(sha256)
    except ArtifactNotFoundError:
        err("CODE_ARTIFACT_UNAVAILABLE", "code artifact is not available", 404)
    except ArtifactIntegrityError:
        err("CODE_ARTIFACT_INTEGRITY", "code artifact integrity check failed", 422)
    if hashlib.sha256(data).hexdigest() != sha256:
        err("CODE_ARTIFACT_INTEGRITY", "code artifact integrity check failed", 422)
    return data


def _experiment_or_404(db, experiment_id: str) -> ExperimentRecord:
    exp = db.get(ExperimentRecord, experiment_id)
    if exp is None:
        err("RESOURCE_NOT_FOUND", "resource not found", 404)
    return exp


@router.post("/experiments/{experiment_id}/codegen", status_code=201)
async def create_codegen(experiment_id: str, request: Request):
    """Generate reproduction code for an experiment via the provider seam.

    The generated script is a reviewed artifact: it passes static AST policy
    validation before it is stored content-addressed, and it is never
    executed on this path. The attempt is experiment-level (run_id NULL,
    experiment_id set) so it exists before any Run; the run-creation gate
    later binds it.
    """
    runtime = request.app.state.runtime
    uid = actor_id(request)
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        err("VALIDATION_ERROR", "invalid JSON")
    if not isinstance(payload, dict):
        err("VALIDATION_ERROR", "invalid payload")
    if not set(payload) <= _ALLOWED_CODEGEN_KEYS:
        err("VALIDATION_ERROR", "unsupported codegen request field",
            422, {"allowed": sorted(_ALLOWED_CODEGEN_KEYS)})
    provider_session_id = payload.get("provider_session_id")
    if provider_session_id is not None and not isinstance(provider_session_id, str):
        err("VALIDATION_ERROR", "provider_session_id must be a string")
    prompt_version = payload.get("prompt_version") or "codegen-prompt-1"
    if not isinstance(prompt_version, str) or not prompt_version:
        err("VALIDATION_ERROR", "prompt_version must be a non-empty string")

    from p2r_orchestration.codegen_service import CodegenPolicyError

    with runtime["session_factory"]() as db:
        exp = _experiment_or_404(db, experiment_id)
        write_check(request, db, exp.project_id, uid)
        key = request.headers.get("idempotency-key")
        if not key:
            err("VALIDATION_ERROR", "Idempotency-Key is required")
        claim = IdempotencyRepository(db).claim(scope=f"codegen:{uid}:{experiment_id}", key=key, body=raw)
        if claim.conflict:
            err("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", 409)
        if claim.record.response_json is not None:
            return envelope(request, claim.record.response_json)
        if not claim.claimed:
            err("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", 409)
        try:
            paper = db.get(PaperRecord, exp.paper_id)
            if paper is None:
                err("RESOURCE_NOT_FOUND", "resource not found", 404)
            extraction = db.get(ExtractionRecord, exp.extraction_id)
            extraction_payload = dict(extraction.payload_json or {}) if extraction is not None else {}
            head = db.get(SpecRevisionRecord, exp.head_revision_id) if exp.head_revision_id else None
            spec = dict(head.spec_json or {}) if head is not None else {}
            service, provider_config_id, model_id = _resolve_codegen_service(runtime, provider_session_id)
            current = db.scalar(
                select(func.max(AttemptRecord.attempt_index)).where(
                    AttemptRecord.experiment_id == experiment_id,
                    AttemptRecord.run_id.is_(None),
                )
            )
            attempt_index = (current or 0) + 1
            try:
                result = await service.generate(
                    project_id=exp.project_id,
                    experiment_id=exp.id,
                    revision_id=head.id if head is not None else None,
                    paper_sha256=paper.sha256,
                    extraction_payload=extraction_payload,
                    spec=spec,
                    provider_config_id=provider_config_id,
                    model_id=model_id,
                    call_id=f"codegen-{exp.id}-{attempt_index:06d}",
                    prompt_version=prompt_version,
                    policy_version=_CODEGEN_POLICY_VERSION,
                )
            except CodegenPolicyError as exc:
                err("CODEGEN_POLICY_REJECTED", "generated code was rejected by the static safety policy",
                    422, {"reason": str(exc)})
            except Exception as exc:  # ProviderError or anything the provider raised
                code = "PROVIDER_" + _provider_code(exc).upper()
                error = ServiceError("codegen provider call failed")
                error.code, error.status = code, 422
                raise error from None
            manifest = {
                "code_artifact_id": result["code_artifact_id"],
                "code_sha256": result["code_sha256"],
                "generator_model": model_id,
                "prompt_version": prompt_version,
                "policy_version": _CODEGEN_POLICY_VERSION,
            }
            row = AttemptRecord(
                experiment_id=exp.id,
                run_id=None,
                attempt_index=attempt_index,
                source="model",
                status="candidate",
                base_code_hash=result["code_sha256"],
                validation_json={},
                code_manifest_json=manifest,
            )
            db.add(row)
            db.flush()
            out = {
                "attempt_id": row.id,
                "attempt_index": row.attempt_index,
                "code": result["code"],
                "code_sha256": result["code_sha256"],
                "notes": result["notes"],
            }
            claim.record.response_json = out
            db.commit()
            return envelope(request, out)
        except BaseException:
            # A failed generation must not poison the idempotency key: drop
            # the claim so an honest retry (regenerate after rejection) can
            # reuse or replace it. Same cleanup pattern as repair proposals.
            db.rollback()
            if claim.claimed:
                db.execute(delete(IdempotencyKey).where(IdempotencyKey.id == claim.record.id))
                db.commit()
            raise


@router.get("/experiments/{experiment_id}/attempts/{attempt_index}/code")
def get_attempt_code(experiment_id: str, attempt_index: int, request: Request):
    """Read back the full reviewed code and manifest of a model attempt."""
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        exp = _experiment_or_404(db, experiment_id)
        require_project_access(db, actor_id(request), exp.project_id)
        row = db.scalar(
            select(AttemptRecord).where(
                AttemptRecord.experiment_id == experiment_id,
                AttemptRecord.run_id.is_(None),
                AttemptRecord.attempt_index == attempt_index,
            ).order_by(AttemptRecord.created_at.desc(), AttemptRecord.id.desc())
        )
        if row is None:
            err("RESOURCE_NOT_FOUND", "resource not found", 404)
        manifest = dict(row.code_manifest_json or {})
        code_sha256 = manifest.get("code_sha256") or row.base_code_hash
        if not isinstance(code_sha256, str) or len(code_sha256) != 64:
            err("CODE_ARTIFACT_UNAVAILABLE", "attempt has no code artifact", 404)
        data = _read_artifact(runtime, code_sha256)
        return envelope(request, {
            "attempt_id": row.id,
            "experiment_id": experiment_id,
            "attempt_index": row.attempt_index,
            "source": row.source,
            "status": row.status,
            "code": data.decode("utf-8"),
            "code_sha256": code_sha256,
            "manifest": manifest,
        })
