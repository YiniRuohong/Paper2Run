from __future__ import annotations

import hashlib
from typing import Any
from sqlalchemy import select
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    ExportArtifactRecord,
    ExportItemRecord,
    ExportRecord,
    OperationRecord,
    ReportRecord,
)
from .export_builder import build_bundle, canonical_json, render_markdown

RULES_VERSION = "guided-export-v1"
MEDIA = {"report_json": "application/json", "report_markdown": "text/markdown", "bundle_zip": "application/zip"}
FILENAMES = {"report_json": "report.json", "report_markdown": "report.md", "bundle_zip": "report-bundle.zip"}

# Public errors are fixed codes only. Never propagate str(exc): backend
# messages may carry paths, provider configuration or other secrets.
_ERROR_EXPORT_FAILED = "EXPORT_FAILED"
_ERROR_REPORT_INVALID = "EXPORT_FAILED"
_ERROR_INPUT_INVALID = "EXPORT_INPUT_INVALID"
_ERROR_INCOMPLETE = "EXPORT_INCOMPLETE"
_ERROR_EVIDENCE_INVALID = "EXPORT_EVIDENCE_INVALID"


class _SafeError(Exception):
    """Internal carrier for a fixed public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def reuse_key(run_id: str, report_hash: str, fmt: str) -> str:
    value = {"run_id": run_id, "report_hash": report_hash, "format": fmt, "rules_version": RULES_VERSION}
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _set_projection(export: ExportRecord, items: list[ExportItemRecord]) -> None:
    payload = [
        {
            "format": i.format,
            "status": i.status,
            "artifact_id": i.artifact_id,
            **({"error": i.error_json} if i.error_json else {}),
        }
        for i in items
    ]
    export.items_json = payload
    if not items:
        export.status = "failed"
        return
    ready = sum(i.status == "ready" for i in items)
    failed = sum(i.status == "failed" for i in items)
    export.status = "succeeded" if ready == len(items) else ("failed" if failed == len(items) else "partial")
    first = next((i for i in items if i.format == "report_json" and i.status == "ready"), None)
    if first:
        export.artifact_id = first.artifact_id
        export.storage_key = first.storage_key
        export.sha256 = first.sha256
        export.bytes = first.bytes
        export.media_type = first.media_type
    else:
        export.artifact_id = None
        export.storage_key = None
        export.sha256 = None
        export.bytes = None
        export.media_type = None


def _update_operation(db, export: ExportRecord, status: str, code: str | None = None) -> None:
    if not export.operation_id:
        return
    operation = db.get(OperationRecord, export.operation_id)
    if operation is None:
        return
    operation.status = status
    operation.error_json = {"code": code} if code else None


def _validated_evidence(db, store, export: ExportRecord, report_json: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Bundle evidence comes only from this Run's frozen, registered entries."""
    evidence = report_json["evidence"]
    if not isinstance(evidence, list) or not evidence:
        # Controlled smoke honestly registers no evidence; refuse to ship a
        # ZIP that would claim a complete reproduction bundle.
        raise _SafeError(_ERROR_INCOMPLETE)
    entries: list[tuple[str, bytes]] = []
    for entry in evidence:
        if not isinstance(entry, dict):
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        required = ("run_id", "sha256", "bytes", "media_type")
        if any(key not in entry for key in required):
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        run_id, sha256, nbytes, media_type = (entry[key] for key in required)
        if run_id != export.run_id:
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        if not _is_hash(sha256) or not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 0:
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        if not isinstance(media_type, str) or not media_type or "/" not in media_type:
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        try:
            content = store.open_bytes(sha256)
        except Exception as exc:
            raise _SafeError(_ERROR_EVIDENCE_INVALID) from exc
        if hashlib.sha256(content).hexdigest() != sha256 or len(content) != nbytes:
            raise _SafeError(_ERROR_EVIDENCE_INVALID)
        entries.append((f"evidence/{sha256}.txt", content))
    return entries


def _build_content(db, store, export: ExportRecord, item: ExportItemRecord, report_json: dict[str, Any]) -> bytes:
    if item.format == "report_json":
        return canonical_json(report_json)
    if item.format == "report_markdown":
        try:
            return render_markdown(report_json)
        except ValueError as exc:
            raise _SafeError(_ERROR_INPUT_INVALID) from exc
    if item.format == "bundle_zip":
        required = {"run_id", "manifest", "metrics", "evidence", "limitations", "metrics_source"}
        if not isinstance(report_json, dict) or not required.issubset(report_json):
            raise _SafeError(_ERROR_INPUT_INVALID)
        manifest = report_json["manifest"]
        metrics = report_json["metrics"]
        if not isinstance(manifest, dict) or not isinstance(metrics, dict) or not isinstance(report_json["evidence"], list):
            raise _SafeError(_ERROR_INPUT_INVALID)
        evidence = _validated_evidence(db, store, export, report_json)
        try:
            return build_bundle(report_json, manifest=manifest, metrics=metrics, evidence=evidence)
        except ValueError as exc:
            raise _SafeError(_ERROR_INPUT_INVALID) from exc
    raise _SafeError(_ERROR_EXPORT_FAILED)


def _reuse_valid(cached: ExportArtifactRecord | None, *, key: str, export: ExportRecord, item: ExportItemRecord, store) -> bool:
    """Full metadata re-validation before any reuse; never trust old ready state."""
    if cached is None:
        return False
    if (
        cached.reuse_key != key
        or cached.run_id != export.run_id
        or cached.report_hash != export.report_hash
        or cached.format != item.format
        or cached.rules_version != RULES_VERSION
        or cached.media_type != MEDIA.get(item.format)
        or cached.filename != FILENAMES.get(item.format)
        or not _is_hash(cached.artifact_id)
        or cached.sha256 != cached.artifact_id
        or not isinstance(cached.bytes, int)
        or isinstance(cached.bytes, bool)
        or cached.bytes < 0
        or not isinstance(cached.storage_key, str)
        or not cached.storage_key
    ):
        return False
    path_fn = getattr(store, "_path", None)
    if path_fn is None:
        return False
    try:
        if str(path_fn(cached.artifact_id)) != cached.storage_key:
            return False
        content = store.open_bytes(cached.artifact_id)
    except Exception:
        return False
    return hashlib.sha256(content).hexdigest() == cached.sha256 and len(content) == cached.bytes


def process_claimed_export_job(
    session_factory, artifact_store, *, export_id, job_id, worker_id, token,
) -> bool:
    return _process_claimed_export_job(
        session_factory, artifact_store, export_id=export_id, job_id=job_id,
        worker_id=worker_id, token=token,
    )


def process_export_job(session_factory, artifact_store, *, worker_id="export-worker") -> bool:
    with session_factory() as db:
        job = JobRepository(db).claim(worker_id=worker_id, kinds=("export_bundle", "export_report_json"))
        if not job:
            db.commit()
            return False
        export_id, job_id, token = job.payload_ref, job.id, job.fencing_token
        db.commit()
    return _process_claimed_export_job(
        session_factory, artifact_store, export_id=export_id, job_id=job_id,
        worker_id=worker_id, token=token,
    )


def _process_claimed_export_job(
    session_factory, artifact_store, *, export_id, job_id, worker_id, token,
) -> bool:
    with session_factory() as db:
        export = db.get(ExportRecord, export_id)
        if not export:
            JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="export not found")
            db.commit()
            return False
        _update_operation(db, export, "running")
        items = db.scalars(
            select(ExportItemRecord)
            .where(ExportItemRecord.export_id == export.id)
            .order_by(ExportItemRecord.format)
        ).all()
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == export.run_id))
        public_error: str | None = None
        if report is None or not export.report_hash or report.content_hash != export.report_hash:
            public_error = _ERROR_REPORT_INVALID
        if public_error is not None:
            for item in items:
                item.status = "failed"
                item.error_json = {"code": public_error}
            export.status = "failed"
            export.error_json = {"code": public_error}
            _update_operation(db, export, "failed", public_error)
            if not JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="export failed"):
                db.rollback()
                JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token)
                db.commit()
                return False
            db.commit()
            return True
        report_json = report.content_json
        for item in items:
            if item.status in {"ready", "failed"}:
                continue
            item.status = "running"
            if item.format == "report_pdf":
                item.status = "failed"
                item.error_json = {"code": "PDF_UNAVAILABLE"}
                continue
            try:
                key = item.reuse_key or reuse_key(export.run_id, export.report_hash, item.format)
                item.reuse_key = key
                cached = db.get(ExportArtifactRecord, key)
                reuse_ok = _reuse_valid(cached, key=key, export=export, item=item, store=artifact_store)
                if not reuse_ok:
                    content = _build_content(db, artifact_store, export, item, report_json)
                    address = artifact_store.put_bytes(content)
                    if cached is None:
                        cached = ExportArtifactRecord(
                            reuse_key=key,
                            run_id=export.run_id,
                            report_hash=export.report_hash,
                            format=item.format,
                            rules_version=RULES_VERSION,
                            artifact_id=address.sha256,
                            storage_key=str(address.path),
                            sha256=address.sha256,
                            bytes=address.bytes,
                            media_type=MEDIA[item.format],
                            filename=FILENAMES[item.format],
                        )
                        db.add(cached)
                    else:
                        cached.run_id = export.run_id
                        cached.report_hash = export.report_hash
                        cached.format = item.format
                        cached.rules_version = RULES_VERSION
                        cached.artifact_id = address.sha256
                        cached.storage_key = str(address.path)
                        cached.sha256 = address.sha256
                        cached.bytes = address.bytes
                        cached.media_type = MEDIA[item.format]
                        cached.filename = FILENAMES[item.format]
                    db.flush()
                item.status = "ready"
                item.artifact_id = cached.artifact_id
                item.storage_key = cached.storage_key
                item.sha256 = cached.sha256
                item.bytes = cached.bytes
                item.media_type = cached.media_type
                item.filename = cached.filename
                item.error_json = None
            except _SafeError as exc:
                item.status = "failed"
                item.error_json = {"code": exc.code}
            except Exception:
                item.status = "failed"
                item.error_json = {"code": _ERROR_EXPORT_FAILED}
        _set_projection(export, items)
        if export.status == "failed":
            _update_operation(db, export, "failed", export.error_json["code"] if export.error_json else _ERROR_EXPORT_FAILED)
            ok = JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error="export failed")
        else:
            # Partial means at least one artifact is deliverable; per-item
            # errors stay on the items and the job completes normally.
            _update_operation(db, export, "succeeded")
            ok = JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token)
        if not ok:
            db.rollback()
            JobRepository(db).requeue_owned(job_id, worker_id=worker_id, fencing_token=token)
            db.commit()
            return False
        db.commit()
        return True
