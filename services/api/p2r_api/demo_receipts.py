"""Demo-only, transaction-owned write receipts. Call after authorization, before commit."""
from hashlib import sha256
import re

from p2r_orchestration.service import ServiceError
from p2r_persistence.models import DemoWriteReceiptRecord
from .auth import require_project_access
from .dependencies import actor_id

_PATHS = (
    r'/api/v1/demo/bootstrap',
    r'/api/v1/projects/[^/]+/(experiments|spec-revisions|runs)',
    r'/api/v1/projects/[^/]+/experiments/[^/]+/runs',
    r'/api/v1/spec-revisions/[^/]+/confirm',
    r'/api/v1/runs/[^/]+/(cancel|exports|report-builds)',
)


def error(code, message, status):
    exc = ServiceError(message)
    exc.code, exc.status = code, status
    raise exc


def is_allowed_path(path):
    return any(re.fullmatch(pattern, path) for pattern in _PATHS)


def enabled(request):
    return request.app.state.runtime.get('demo_enabled') and is_allowed_path(request.url.path)


def lookup(request, db, raw):
    if not enabled(request):
        return None
    # SQLite's legacy transaction mode does not begin on SELECT. Acquire the
    # write transaction before checking the global identity, also ensuring that
    # existing repository SAVEPOINTs cannot commit an otherwise empty outer tx.
    connection = db.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    key = request.headers.get('idempotency-key')
    if not key:
        error('VALIDATION_ERROR', 'Idempotency-Key is required', 422)
    row = db.get(DemoWriteReceiptRecord, (actor_id(request), key))
    if row is None:
        return None
    if row.project_id:
        require_project_access(db, actor_id(request), row.project_id)
    if row.target_path != request.url.path or row.request_sha256 != sha256(raw).hexdigest():
        error('IDEMPOTENCY_CONFLICT', 'idempotency key was used with a different request', 409)
    return row.response_json


def save(request, db, raw, project_id, response):
    if not enabled(request):
        return
    db.add(DemoWriteReceiptRecord(
        actor_id=actor_id(request), key=request.headers['idempotency-key'],
        target_path=request.url.path, request_sha256=sha256(raw).hexdigest(),
        project_id=project_id, response_json=response,
    ))
    db.flush()
