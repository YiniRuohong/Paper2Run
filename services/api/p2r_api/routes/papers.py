from __future__ import annotations

import hashlib
import os
import re
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select

from sqlalchemy.orm import object_session
from p2r_artifacts.store import LocalArtifactStore, ArtifactIntegrityError, ArtifactNotFoundError
from p2r_docparse.errors import DocParseError
from p2r_docparse.pdf import count_pages
from p2r_orchestration.docparse_jobs import enqueue_paper_parse
from p2r_orchestration.errors import ServiceError
from p2r_persistence.models import IdempotencyKey, OperationRecord, PaperRecord, PaperVersionRecord, ExtractionRecord
from p2r_persistence.repositories import IdempotencyRepository
from ..auth import csrf_token, validate_csrf, validate_origin, require_project_access
from ..dependencies import actor_id

router = APIRouter()
_PDF_MAGIC = b"%PDF-"


def _envelope(request: Request, data: Any):
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def _error(code: str, message: str, *, status: int = 422, details: dict | None = None):
    err = ServiceError(message, details=details)
    err.status, err.code = status, code
    raise err


def _claim(request, db, *, scope: str, key: str, body: bytes):
    """Claim an idempotency key; return the stored response for a replay, else None."""
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
    """Record the response of a claimed idempotency key (the caller commits)."""
    row = db.scalar(select(IdempotencyKey).where(
        IdempotencyKey.scope == scope, IdempotencyKey.key == key))
    if row is not None:
        row.response_json = response


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
            "source_kind": row.source_kind, "media_type": row.media_type,
            "content_available": True, "current_extraction_id": extraction.id if extraction else None,
            "active_operation_id": operation.id if operation else None, "processing_consent": {"accepted_at": row.created_at.isoformat(), "policy_version": "1"},
            "error": row.error_json, "created_at": row.created_at.isoformat()}


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
