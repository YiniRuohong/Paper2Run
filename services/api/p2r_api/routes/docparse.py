from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from p2r_docparse.errors import DocParseError
from p2r_docparse.platform import current_platform
from p2r_orchestration.docparse_jobs import enqueue_paper_parse
from p2r_persistence.models import PaperRecord, PaperTextRecord

from ..auth import require_project_access
from ..dependencies import actor_id
from .papers import _check_write, _envelope, _error

router = APIRouter()
_PREVIEW_CHARS = 4000


def _service(request: Request):
    return request.app.state.runtime["docparse_service"]


def _status(request: Request) -> dict:
    service = _service(request)
    return {"mode": service.mode(), "offline": service.installer.state().to_json(),
            "platform": current_platform()}


@router.get("/docparse/status")
def docparse_status(request: Request):
    actor_id(request)
    return _envelope(request, _status(request))


@router.put("/docparse/mode")
async def set_docparse_mode(request: Request):
    actor_id(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("online", "offline"):
        _error("VALIDATION_ERROR", "mode must be 'online' or 'offline'")
    _service(request).settings.set_mode(mode)
    return _envelope(request, _status(request))


@router.post("/docparse/offline/install", status_code=202)
def install_offline(request: Request):
    actor_id(request)
    try:
        _service(request).installer.start()
    except DocParseError as exc:
        if exc.code == "DOCPARSE_OFFLINE_UNSUPPORTED":
            _error(exc.code, "offline parsing is not supported on this platform", status=409)
        raise
    return _envelope(request, _status(request))


@router.post("/docparse/offline/cancel")
def cancel_offline(request: Request):
    actor_id(request)
    _service(request).installer.cancel()
    return _envelope(request, _status(request))


@router.delete("/docparse/offline")
def uninstall_offline(request: Request):
    actor_id(request)
    _service(request).installer.uninstall()
    return _envelope(request, _status(request))


@router.post("/papers/{paper_id}/parse", status_code=202)
def parse_paper(paper_id: str, request: Request):
    runtime = request.app.state.runtime
    user_id = actor_id(request)
    with runtime["session_factory"]() as db:
        paper = db.get(PaperRecord, paper_id)
        if paper is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        _check_write(request, db, paper.project_id, user_id)
        key = request.headers.get("idempotency-key")
        if not key: _error("VALIDATION_ERROR", "Idempotency-Key is required")
        if paper.media_type != "application/pdf":
            _error("PAPER_NOT_PDF", "only PDF papers can be parsed", status=409)
        mode = _service(request).mode()
        if mode is None:
            _error("DOCPARSE_MODE_UNSET", "choose online or offline parsing first", status=409)
        job = enqueue_paper_parse(db, paper_id=paper_id, mode=mode,
                                  idempotency_key=f"paper_parse:{paper_id}:{key}")
        db.commit()
        return _envelope(request, {"paper_id": paper_id, "job_id": job.id, "status": "parsing"})


@router.get("/papers/{paper_id}/text")
def paper_text(paper_id: str, request: Request):
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        paper = db.get(PaperRecord, paper_id)
        if paper is None: _error("RESOURCE_NOT_FOUND", "resource not found", status=404)
        require_project_access(db, actor_id(request), paper.project_id)
        record = db.scalar(select(PaperTextRecord).where(
            PaperTextRecord.paper_id == paper_id, PaperTextRecord.source_sha256 == paper.sha256,
        ).order_by(PaperTextRecord.created_at.desc(), PaperTextRecord.id.desc()))
        if record is None:
            return _envelope(request, {"status": "none", "backend": None, "page_count": paper.page_count,
                                       "pages_parsed": 0, "chunks": [], "warnings": [],
                                       "error_code": None, "preview": None})
        preview = None
        if record.status == "ready" and record.text_sha256:
            store = runtime.get("artifact_store")
            if store is not None:
                try:
                    preview = store.open_bytes(record.text_sha256).decode("utf-8", "replace")[:_PREVIEW_CHARS]
                except Exception:
                    preview = None
        return _envelope(request, {
            "status": record.status, "backend": record.backend, "page_count": record.page_count,
            "pages_parsed": record.pages_parsed, "chunks": list(record.chunk_map_json or []),
            "warnings": list(record.warnings_json or []),
            "error_code": (record.error_json or {}).get("code"), "preview": preview})
