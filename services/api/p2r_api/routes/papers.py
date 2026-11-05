from __future__ import annotations

import hashlib
import os
import re
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select

from sqlalchemy.orm import object_session
from p2r_artifacts.store import LocalArtifactStore, ArtifactIntegrityError, ArtifactNotFoundError
from p2r_docparse.errors import DocParseError
from p2r_docparse.pdf import count_pages
from p2r_orchestration.docparse_jobs import enqueue_paper_parse
from p2r_orchestration.service import ServiceError
from p2r_persistence.models import ExperimentRecord, OperationRecord, PaperRecord, PaperVersionRecord, ExtractionRecord, SourceRefRecord
from p2r_persistence.repositories import IdempotencyRepository
from ..auth import csrf_token, validate_csrf, validate_origin, require_project_access
from ..dependencies import actor_id
from ..extraction_payload import ExtractionPayloadError, build_extraction_payload
from ..paper_text import latest_paper_text

router = APIRouter()
_PDF_MAGIC = b"%PDF-"
# ExtractionRequest: the only body fields create_extraction accepts. Unknown
# keys are refused so an api_key (or any credential) can never ride along.
_ALLOWED_EXTRACTION_KEYS = {"reason", "provider_session_id", "prompt_version", "policy_version"}


def _envelope(request: Request, data: Any):
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def _error(code: str, message: str, *, status: int = 422, details: dict | None = None):
    err = ServiceError(message, details=details)
    err.status, err.code = status, code
    raise err


def _parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.I)
    if not match:
        _error("VALIDATION_ERROR", "multipart boundary is required")
    boundary = (match.group(1) or match.group(2)).strip().encode()
    # A delimiter is a complete framing line, not an arbitrary occurrence in
    # binary PDF data. Its leading CRLF belongs to framing; payload CR/LF bytes
    # before that remain part of the source and its content-addressed hash.
    delimiter = re.compile(rb'(?:\A|\r\n)--' + re.escape(boundary)
                           + rb'(?P<closing>--)?[ \t]*(?:\r\n|\Z)')
    markers = list(delimiter.finditer(body))
    if len(markers) < 2 or not markers[-1].group("closing"):
        _error("VALIDATION_ERROR", "multipart closing boundary is required")
    parts = {}
    for current, following in zip(markers, markers[1:]):
        if current.group("closing"):
            break
        chunk = body[current.end():following.start()]
        head, sep, value = chunk.partition(b"\r\n\r\n")
        if not sep: continue
        headers = head.decode("latin1").split("\r\n")
        disposition = next((h for h in headers if h.lower().startswith("content-disposition:")), "")
        name = re.search(r'name="([^"]+)"', disposition)
        if not name: continue
        filename = re.search(r'filename="([^"]*)"', disposition)
        parts[name.group(1)] = (filename.group(1) if filename else None, value)
    return parts


def _paper_json(row: PaperRecord) -> dict:
    db = object_session(row)
    extraction = db.scalar(select(ExtractionRecord).where(ExtractionRecord.paper_id == row.id).order_by(ExtractionRecord.created_at.desc()))
    operation = db.scalar(select(OperationRecord).where(OperationRecord.resource_id == row.id, OperationRecord.status.in_(['queued', 'running'])).order_by(OperationRecord.created_at.desc()))
    version = db.scalar(select(PaperVersionRecord).where(
        PaperVersionRecord.paper_id == row.id, PaperVersionRecord.sha256 == row.sha256,
    ).order_by(PaperVersionRecord.version.desc()))
    return {"paper_id": row.id, "paper_version_id": version.id if version else None,
            "project_id": row.project_id, "title": row.title,
            "status": row.status, "sha256": row.sha256, "page_count": row.page_count,
            "content_available": True, "current_extraction_id": extraction.id if extraction else None,
            "active_operation_id": operation.id if operation else None, "processing_consent": {"accepted_at": row.created_at.isoformat(), "policy_version": "1"},
            "error": row.error_json, "created_at": row.created_at.isoformat()}


def _operation_json(row: OperationRecord) -> dict:
    return {"operation_id": row.id, "kind": row.kind, "resource_id": row.resource_id, "status": row.status, "error": row.error_json}


def _check_write(request: Request, db, project_id: str, user_id: str):
    project, _ = require_project_access(db, user_id, project_id, action="write")
    if not request.app.state.runtime.get("development_auth"):
        allowed = request.app.state.runtime.get("allowed_origin")
        if not allowed: _error("CSRF_FAILED", "origin rejected", status=403)
        validate_origin(request.headers.get("origin"), allowed)
        secret = request.cookies.get("p2r_session")
        if not secret: _error("SESSION_REQUIRED", "session required", status=401)
        validate_csrf(request.headers.get("x-csrf-token"), csrf_token(secret))
    return project


@router.post("/projects/{project_id}/papers", status_code=202)
async def upload_paper(project_id: str, request: Request):
    runtime = request.app.state.runtime
    user_id = actor_id(request)
    with runtime["session_factory"]() as db:
        _check_write(request, db, project_id, user_id)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        body = await request.body()
        claim = IdempotencyRepository(db).claim(scope=f"paper-upload:{user_id}:{project_id}", key=key, body=body)
        if claim.conflict: _error("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", status=409)
        if claim.record.response_json is not None: return _envelope(request, claim.record.response_json)
        if not claim.claimed: _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", status=409)
        content_type = request.headers.get("content-type", "")
        fields = _parse_multipart(content_type, body) if content_type.lower().startswith("multipart/") else {}
        file_name, data = fields.get("file", (None, b""))
        consent = fields.get("source_processing_consent", (None, b""))[1].decode("utf-8", "ignore").lower()
        if consent != "true": _error("SOURCE_PROCESSING_CONSENT_REQUIRED", "source_processing_consent must be true")
        max_bytes = int(os.getenv("P2R_PAPER_MAX_BYTES", str(64 * 1024 * 1024)))
        max_pages = int(os.getenv("P2R_PAPER_MAX_PAGES", "500"))
        if not file_name or not data.startswith(_PDF_MAGIC): _error("PDF_REQUIRED", "file must be a PDF")
        if len(data) > max_bytes: _error("PDF_TOO_LARGE", "PDF exceeds configured byte limit", details={"max_bytes": max_bytes})
        try:
            pages = count_pages(data)
        except DocParseError:
            _error("PDF_INVALID", "PDF could not be read")
        if pages < 1: _error("PDF_INVALID", "PDF contains no pages")
        if pages > max_pages: _error("PDF_TOO_MANY_PAGES", "PDF exceeds configured page limit", details={"max_pages": max_pages})
        digest = hashlib.sha256(data).hexdigest()
        store = runtime.get("artifact_store")
        if store is None:
            store = LocalArtifactStore(Path(os.getenv("P2R_ARTIFACT_ROOT", ".p2r-artifacts")), max_bytes=max_bytes)
        address = store.put_bytes(data)
        paper = PaperRecord(project_id=project_id, title=fields.get("title_hint", (None, b""))[1].decode("utf-8", "replace") or None, sha256=digest, artifact_key=str(address.path), bytes=len(data), page_count=pages, processing_consent=True, status="uploaded")
        db.add(paper); db.flush()
        version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=digest, artifact_key=str(address.path), bytes=len(data), page_count=pages)
        operation = OperationRecord(kind="paper_ingestion", resource_id=paper.id, status="succeeded")
        db.add_all([version, operation]); db.flush()
        status = "uploaded"
        service = runtime["docparse_service"]
        mode = service.mode()
        # Offline without a ready component would fail at once; leave the paper for the explicit flow.
        if mode == "online" or (mode == "offline" and service.installer.state().state == "ready"):
            enqueue_paper_parse(db, paper_id=paper.id, mode=mode, idempotency_key=f"paper_parse:{paper.id}:upload")
            status = "parsing"
        payload = {"paper_id": paper.id, "operation_id": operation.id, "status": status}
        claim.record.response_json = payload; db.commit()
        return _envelope(request, payload)


def _insert_paper(runtime, db, *, project_id, title, data, source_kind, media_type, pages, status="uploaded"):
    max_bytes = int(os.getenv("P2R_PAPER_MAX_BYTES", str(64 * 1024 * 1024)))
    digest = hashlib.sha256(data).hexdigest()
    store = runtime.get("artifact_store")
    if store is None:
        store = LocalArtifactStore(Path(os.getenv("P2R_ARTIFACT_ROOT", ".p2r-artifacts")), max_bytes=max_bytes)
    address = store.put_bytes(data)
    paper = PaperRecord(project_id=project_id, title=title, sha256=digest,
                        artifact_key=str(address.path), bytes=len(data), page_count=pages,
                        source_kind=source_kind, media_type=media_type,
                        processing_consent=True, status=status)
    db.add(paper); db.flush()
    version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=digest,
                                 artifact_key=str(address.path), bytes=len(data),
                                 page_count=pages, source_kind=source_kind, media_type=media_type)
    operation = OperationRecord(kind="paper_ingestion", resource_id=paper.id, status="succeeded")
    db.add_all([version, operation]); db.flush()
    return paper, operation


def _parse_text_payload(content_type: str, body: bytes, *, header_consent: str | None = None) -> dict:
    """Accept either a JSON body or a raw text/plain body for pasted text."""
    if content_type.lower().startswith("application/json"):
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            _error("VALIDATION_ERROR", "invalid JSON")
        if not isinstance(payload, dict):
            _error("VALIDATION_ERROR", "invalid payload")
        return payload
    if content_type.lower().startswith("multipart/"):
        fields = _parse_multipart(content_type, body)
        text = fields.get("text", (None, b""))[1]
        consent = fields.get("source_processing_consent", (None, b""))[1].decode("utf-8", "ignore").lower()
        title = fields.get("title_hint", (None, b""))[1].decode("utf-8", "replace")
        return {"text": text, "source_processing_consent": consent == "true", "title_hint": title}
    if content_type.lower().startswith("text/"):
        # Raw text/plain body carries no consent field: require explicit consent
        # via the X-Source-Processing-Consent header (never implicit).
        header_consent = (header_consent or "").lower()
        return {"text": body, "source_processing_consent": header_consent == "true"}
    _error("TEXT_UNSUPPORTED_MEDIA", "text/plain or application/json body is required")


@router.post("/projects/{project_id}/papers/text", status_code=202)
async def create_text_paper(project_id: str, request: Request):
    """Create a Paper from pasted/plain text (TXT). Validates UTF-8, size, consent."""
    runtime = request.app.state.runtime
    user_id = actor_id(request)
    with runtime["session_factory"]() as db:
        _check_write(request, db, project_id, user_id)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        body = await request.body()
        claim = IdempotencyRepository(db).claim(scope=f"paper-text:{user_id}:{project_id}", key=key, body=body)
        if claim.conflict: _error("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", status=409)
        if claim.record.response_json is not None: return _envelope(request, claim.record.response_json)
        if not claim.claimed: _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", status=409)
        content_type = request.headers.get("content-type", "")
        payload = _parse_text_payload(
            content_type, body,
            header_consent=request.headers.get("x-source-processing-consent"),
        )

        consent = payload.get("source_processing_consent")
        if consent is not True and str(consent).lower() != "true":
            _error("SOURCE_PROCESSING_CONSENT_REQUIRED", "source_processing_consent must be true")

        raw_text = payload.get("text")
        if isinstance(raw_text, str):
            raw_bytes = raw_text.encode("utf-8")
        elif isinstance(raw_text, (bytes, bytearray)):
            raw_bytes = bytes(raw_text)
            try:
                raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                _error("TEXT_INVALID_UTF8", "text must be valid UTF-8")
        else:
            raw_bytes = b""
            _error("VALIDATION_ERROR", "text is required")

        if not raw_bytes.strip():
            _error("VALIDATION_ERROR", "text must not be empty")

        max_bytes = int(os.getenv("P2R_TEXT_MAX_BYTES", str(2 * 1024 * 1024)))
        if len(raw_bytes) > max_bytes:
            _error("TEXT_TOO_LARGE", "text exceeds configured byte limit", details={"max_bytes": max_bytes})

        title_hint = payload.get("title_hint")
        if isinstance(title_hint, str):
            title = title_hint[:500] or None
        else:
            title = None

        paper, operation = _insert_paper(
            runtime, db, project_id=project_id, title=title, data=raw_bytes,
            source_kind="text", media_type="text/plain", pages=0,
        )
        payload_out = {"paper_id": paper.id, "operation_id": operation.id, "status": "uploaded"}
        claim.record.response_json = payload_out; db.commit()
        return _envelope(request, payload_out)


@router.get("/projects/{project_id}/papers")
def list_papers(project_id: str, request: Request):
    user_id = actor_id(request); runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        require_project_access(db, user_id, project_id)
        rows = db.scalars(select(PaperRecord).where(PaperRecord.project_id == project_id).order_by(PaperRecord.created_at)).all()
        return _envelope(request, [_paper_json(row) for row in rows])


@router.get("/papers/{paper_id}")
def get_paper(paper_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        row = db.get(PaperRecord, paper_id)
        if row is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        require_project_access(db, actor_id(request), row.project_id)
        return _envelope(request, _paper_json(row))


def _source_bytes(runtime, row):
    try:
        store = runtime.get('artifact_store')
        data = store.open_bytes(row.sha256) if store is not None else Path(row.artifact_key).read_bytes()
    except (OSError, ArtifactNotFoundError):
        _error('PAPER_CONTENT_UNAVAILABLE', 'paper content is unavailable', status=404)
    except ArtifactIntegrityError:
        _error('PAPER_CONTENT_INTEGRITY', 'paper content integrity check failed')
    if len(data) != row.bytes or hashlib.sha256(data).hexdigest() != row.sha256:
        _error('PAPER_CONTENT_INTEGRITY', 'paper content integrity check failed')
    return data


def _paper_text_bytes(runtime, record) -> bytes:
    store = runtime.get('artifact_store')
    if store is None:
        _error('PAPER_CONTENT_UNAVAILABLE', 'paper content is unavailable', status=404)
    try:
        return store.open_bytes(record.text_sha256)
    except (OSError, ArtifactNotFoundError):
        _error('PAPER_CONTENT_UNAVAILABLE', 'paper content is unavailable', status=404)
    except ArtifactIntegrityError:
        _error('PAPER_CONTENT_INTEGRITY', 'paper content integrity check failed')


@router.get("/papers/{paper_id}/content")
def paper_content(paper_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        row = db.get(PaperRecord, paper_id)
        if row is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        require_project_access(db, actor_id(request), row.project_id)
        data = _source_bytes(request.app.state.runtime, row)
        total = len(data)
        range_header = request.headers.get("range")
        headers = {"Accept-Ranges": "bytes", "Content-Disposition": "inline", "Content-Type": row.media_type}
        if not range_header:
            headers["Content-Length"] = str(total)
            return Response(data, headers=headers, media_type=row.media_type)
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match or (not match.group(1) and not match.group(2)):
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
        if match.group(1):
            start = int(match.group(1)); end = int(match.group(2) or total - 1)
        else:
            length = int(match.group(2)); start = max(total - length, 0); end = total - 1
        if start >= total or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
        end = min(end, total - 1); chunk = data[start:end + 1]
        headers.update({"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{total}"})
        return Response(chunk, status_code=206, headers=headers, media_type=row.media_type)


def _extract_pdf(data: bytes, paper: PaperRecord) -> tuple[dict, list[SourceRefRecord], str | None]:
    digest = hashlib.sha256(data).hexdigest()
    # Deterministic conservative extraction: only decode literal PDF text strings.
    strings = [s.decode("latin1", "ignore").strip() for s in re.findall(rb"\(([^()]{3,500})\)", data)]
    strings = [s for s in strings if any(c.isalnum() for c in s)]
    if not strings:
        return {}, [], "PDF_TEXT_UNAVAILABLE"
    refs = []
    for page, quote in enumerate(strings[:paper.page_count], 1):
        refs.append(SourceRefRecord(paper_id=paper.id, extraction_id="pending", pdf_sha256=digest, page=page, quote=quote, bbox=None))
    payload = {"extraction_id": None, "paper_id": paper.id, "schema_version": "1.0", "fields": {k: {"value": None, "evidence_status": "missing", "source_refs": []} for k in ("task", "dataset", "optimizer")}, "methods": [], "formulas": [], "candidate_experiments": [], "source_refs": [], "warnings": ["Deterministic text extraction only; semantic fields require review."]}
    return payload, refs, None


@router.get("/papers/{paper_id}/extraction")
def get_extraction(paper_id: str, request: Request):
    with request.app.state.runtime["session_factory"]() as db:
        paper = db.get(PaperRecord, paper_id)
        if paper is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        require_project_access(db, actor_id(request), paper.project_id)
        ext = db.scalar(select(ExtractionRecord).where(ExtractionRecord.paper_id == paper_id).order_by(ExtractionRecord.created_at.desc()))
        if ext is None:
            if paper.status == "failed": _error("PAPER_PARSE_FAILED", "paper parse failed", status=422, details=paper.error_json)
            return JSONResponse(_envelope(request, {"paper_id": paper_id, "operation_id": None, "status": "uploaded"}), status_code=202)
        if ext.status == "failed": _error("PAPER_PARSE_FAILED", "paper parse failed", status=422, details=ext.error_json)
        return _envelope(request, ext.payload_json)


def _provider_code(exc: Exception) -> str:
    """Map provider failures to the fixed public error-code allow-list."""
    from p2r_orchestration.providers import ProviderError

    if isinstance(exc, ProviderError):
        code = getattr(exc, "code", None)
        if code in ProviderError._MESSAGES:
            return code
    return "invalid_response"


async def _extract_with_injected(runtime, session_id: str, data: bytes, paper: PaperRecord, prompt_version: str, policy_version: str) -> dict:
    """Run the injectable provider seam (runtime["extraction_service"]).

    Raises ServiceError with a safe code on any failure; nothing is persisted
    by this function. The provider object itself only arrives via runtime
    injection (tests/ops), never from a request body, and the session id comes
    from the runtime secret store lookup — not from request-held credentials.
    """
    from p2r_orchestration.service import ServiceError

    service = runtime.get("extraction_service")
    if service is None or not hasattr(service, "extract"):
        _error("PROVIDER_NOT_CONFIGURED", "extraction provider is not configured", status=409)
    if not isinstance(session_id, str) or not session_id:
        _error("PROVIDER_NOT_CONFIGURED", "extraction provider session is not configured", status=409)
    # Key material is read only from the runtime secret store keyed by the
    # runtime session id; the request never supplies api_key (refused above).
    secret_store = runtime.get("provider_secret_store")
    try:
        if secret_store is not None:
            secret_store.get(session_id)
            config = secret_store.config(session_id)
            provider_config_id, model_id = config.id, config.model_id
        else:
            provider_config_id, model_id = session_id, ""
        if not model_id:
            raise ValueError("model id missing")
    except Exception:
        _error("PROVIDER_SESSION_UNAVAILABLE", "provider session is not available", status=409)
    projection = None
    try:
        projection = await service.extract(
            paper_id=paper.id,
            paper_sha256=paper.sha256,
            content=data,
            provider_config_id=provider_config_id,
            model_id=model_id,
            call_id=f"ext-{paper.id}-{id(data) & 0xffffffff:08x}",
            prompt_version=prompt_version,
            policy_version=policy_version,
        )
    except Exception as exc:  # ProviderError or anything the fake provider raises
        err = ServiceError("extraction provider call failed")
        err.code = "PROVIDER_" + _provider_code(exc).upper()
        err.status = 422
        raise err from None
    return projection


@router.post("/papers/{paper_id}/extractions", status_code=202)
async def create_extraction(paper_id: str, request: Request):
    runtime = request.app.state.runtime; user_id = actor_id(request)
    body = await request.body()
    try: payload = json.loads(body or b"{}")
    except json.JSONDecodeError: _error("VALIDATION_ERROR", "invalid JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("reason"), str) or not payload["reason"].strip(): _error("VALIDATION_ERROR", "reason is required")
    # Body allow-list: no unknown keys, and specifically no api_key — provider
    # credentials come only from the runtime secret store (see below).
    if not set(payload) <= _ALLOWED_EXTRACTION_KEYS: _error("VALIDATION_ERROR", "unsupported extraction request field", details={"allowed": sorted(_ALLOWED_EXTRACTION_KEYS)})
    provider_session_id = payload.get("provider_session_id")
    if provider_session_id is not None and not isinstance(provider_session_id, str): _error("VALIDATION_ERROR", "provider_session_id must be a string")
    prompt_version = payload.get("prompt_version") or "prompt-1"
    policy_version = payload.get("policy_version") or "policy-1"
    if not isinstance(prompt_version, str) or not isinstance(policy_version, str): _error("VALIDATION_ERROR", "prompt_version and policy_version must be strings")
    with runtime["session_factory"]() as db:
        paper = db.get(PaperRecord, paper_id)
        if paper is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        _check_write(request, db, paper.project_id, user_id)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        use_provider = runtime.get("extraction_service") is not None and provider_session_id is not None
        text_record = None
        if use_provider and paper.media_type == "application/pdf":
            # A PDF reaches the model as its parsed Markdown, never raw bytes.
            # Checked before the idempotency claim: on SQLite the claim's
            # SAVEPOINT commits at once, so a 409 raised after it would leave
            # the key stuck in progress instead of retryable once parsing ends.
            text_record = latest_paper_text(db, paper, status="ready")
            if text_record is None or not text_record.text_sha256:
                _error("PAPER_TEXT_NOT_READY", "parsed paper text is not ready", status=409)
        claim = IdempotencyRepository(db).claim(scope=f"paper-extraction:{user_id}:{paper_id}", key=key, body=body)
        if claim.conflict: _error("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request body", status=409)
        if claim.record.response_json is not None: return _envelope(request, claim.record.response_json)
        if not claim.claimed: _error("IDEMPOTENCY_IN_PROGRESS", "idempotency key is already in progress", status=409)
        data = _source_bytes(runtime, paper)

        # Provider seam: active only when a service is injected into runtime
        # (and, for this request, a live provider session is named). The
        # session id is passed through the process-local runtime, never stored
        # on the request object that downstream helpers receive.
        if use_provider:
            session_store = runtime.get("provider_secret_store")
            if session_store is None or provider_session_id not in session_store:
                _error("PROVIDER_SESSION_UNAVAILABLE", "provider session is not available", status=409)
            if text_record is not None:
                data = _paper_text_bytes(runtime, text_record)
            projection = await _extract_with_injected(runtime, provider_session_id, data, paper, prompt_version, policy_version)
            if text_record is not None and isinstance(projection, dict):
                projection = {**projection, "source_text_sha256": text_record.text_sha256,
                              "parse_backend": text_record.backend}
            try:
                extracted = build_extraction_payload(projection, paper_id=paper.id, paper_sha256=paper.sha256)
            except ExtractionPayloadError:
                _error("PROVIDER_INVALID_RESPONSE", "extraction references another source", status=422)
            if text_record is not None:
                for warning in text_record.warnings_json or []:
                    if warning not in extracted["warnings"]:
                        extracted["warnings"].append(warning)
            # Re-extraction: one extraction row per (paper, content). Replace an
            # earlier row only while no experiment cites it; a cited extraction
            # is immutable provenance and must never change underneath it.
            previous = db.scalar(select(ExtractionRecord).where(
                ExtractionRecord.paper_id == paper.id, ExtractionRecord.content_sha256 == paper.sha256))
            if previous is not None:
                if db.scalar(select(ExperimentRecord.id).where(ExperimentRecord.extraction_id == previous.id).limit(1)) is not None:
                    _error("EXTRACTION_IN_USE", "the current extraction is used by an experiment and cannot be replaced", status=409)
                db.execute(delete(SourceRefRecord).where(SourceRefRecord.extraction_id == previous.id))
                db.delete(previous); db.flush()
            operation = OperationRecord(kind="paper_extraction", resource_id=paper.id, status="succeeded")
            db.add(operation); db.flush()
            ext = ExtractionRecord(paper_id=paper.id, content_sha256=paper.sha256, status="ready", payload_json=extracted)
            db.add(ext); db.flush()
            extracted = dict(extracted); extracted["extraction_id"] = ext.id
            ext.payload_json = extracted; paper.status = "ready"; paper.error_json = None
            response = {"paper_id": paper.id, "operation_id": operation.id, "status": paper.status, "extraction_id": ext.id}
            claim.record.response_json = response; db.commit(); return _envelope(request, response)

        if paper.media_type == 'text/plain':
            ext = db.scalar(select(ExtractionRecord).where(ExtractionRecord.paper_id == paper.id, ExtractionRecord.content_sha256 == paper.sha256))
            if ext is None:
                _error('PAPER_PARSE_FAILED', 'text fixture extraction is unavailable')
            response = {'paper_id': paper.id, 'operation_id': None, 'status': paper.status, 'extraction_id': ext.id}
            claim.record.response_json = response
            db.commit()
            return _envelope(request, response)
        extracted, refs, issue = _extract_pdf(data, paper)
        operation = OperationRecord(kind="paper_extraction", resource_id=paper.id, status="succeeded" if not issue else "failed", error_json={"code": issue} if issue else None)
        db.add(operation); db.flush()
        if issue:
            ext = ExtractionRecord(paper_id=paper.id, content_sha256=paper.sha256, status="failed", payload_json={}, error_json={"code": issue})
            db.add(ext); paper.status = "failed"; paper.error_json = {"code": issue}
        else:
            ext = ExtractionRecord(paper_id=paper.id, content_sha256=paper.sha256, status="ready", payload_json=extracted)
            db.add(ext); db.flush(); extracted = dict(extracted); extracted["extraction_id"] = ext.id
            for ref in refs: ref.extraction_id = ext.id; db.add(ref)
            db.flush()
            extracted["source_refs"] = [{"id": ref.id, "paper_id": ref.paper_id, "pdf_sha256": ref.pdf_sha256, "page": ref.page, "section": None, "table": None, "row": None, "quote": ref.quote, "bbox": None} for ref in refs]
            ext.payload_json = extracted; paper.status = "ready"
        response = {"paper_id": paper.id, "operation_id": operation.id, "status": paper.status, "extraction_id": getattr(ext, "id", None)}
        claim.record.response_json = response; db.commit(); return _envelope(request, response)

