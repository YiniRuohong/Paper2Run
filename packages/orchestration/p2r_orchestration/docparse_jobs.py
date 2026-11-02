from __future__ import annotations

import threading

from sqlalchemy import select

from p2r_docparse.errors import DocParseError
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import PaperRecord, PaperTextRecord

from .docparse_service import DocParseService

PARSE_JOB_KIND = "paper_parse"
_LEASE_SECONDS = 60
_STOP_TIMEOUT_SECONDS = 2.0
_FALLBACK_CODES = {"online": "DOCPARSE_REMOTE_FAILED", "offline": "DOCPARSE_OFFLINE_FAILED"}


def enqueue_paper_parse(db, *, paper_id: str, mode: str, idempotency_key: str):
    """Queue a parse; the mode is locked into the payload at this moment."""
    paper = db.get(PaperRecord, paper_id)
    if paper is None:
        raise KeyError(paper_id)
    job = JobRepository(db).enqueue(kind=PARSE_JOB_KIND, payload_ref=f"{paper_id}:{mode}",
                                    idempotency_key=idempotency_key)
    already = db.scalar(select(PaperTextRecord.id).where(
        PaperTextRecord.paper_id == paper_id, PaperTextRecord.status == "parsing"))
    if already is None or paper.status != "parsing":
        db.add(PaperTextRecord(paper_id=paper_id, source_sha256=paper.sha256, page_count=paper.page_count,
                               status="parsing"))
    paper.status = "parsing"
    return job


def _previous_markdown(db, artifact_store, paper: PaperRecord) -> str | None:
    record = db.scalar(select(PaperTextRecord).where(
        PaperTextRecord.paper_id == paper.id, PaperTextRecord.status == "ready",
        PaperTextRecord.source_sha256 == paper.sha256,
    ).order_by(PaperTextRecord.created_at.desc()))
    if record is None or not record.warnings_json or not record.text_sha256:
        return None
    try:
        return artifact_store.open_bytes(record.text_sha256).decode("utf-8")
    except Exception:
        return None


def _latest_parsing(db, paper_id: str) -> PaperTextRecord | None:
    return db.scalar(select(PaperTextRecord).where(
        PaperTextRecord.paper_id == paper_id, PaperTextRecord.status == "parsing",
    ).order_by(PaperTextRecord.created_at.desc()))


def _heartbeat_loop(session_factory, job_id, worker_id, token, interval, stop, lost) -> None:
    while not stop.wait(interval):
        try:
            with session_factory() as db:
                ok = JobRepository(db).heartbeat(job_id, worker_id, token, lease_seconds=_LEASE_SECONDS)
                db.commit()
        except Exception:
            continue
        if not ok:
            lost.set()
            return


def _record_progress(session_factory, text_id: str):
    def on_progress(done: int, total: int) -> None:
        chunk_map = [{"status": "done"} if i < done else {"status": "pending"} for i in range(total)]
        with session_factory() as db:
            record = db.get(PaperTextRecord, text_id)
            if record is not None:
                record.chunk_map_json = chunk_map
                db.commit()
    return on_progress


def process_next_parse_job(session_factory, artifact_store, service: DocParseService, *,
                           worker_id: str = "docparse", heartbeat_seconds: float = 20.0) -> bool:
    with session_factory() as db:
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        job = JobRepository(db).claim(worker_id=worker_id, lease_seconds=_LEASE_SECONDS,
                                      kinds=(PARSE_JOB_KIND,))
        if job is None:
            db.commit()
            return False
        job_id, token, payload_ref = job.id, job.fencing_token, job.payload_ref
        db.commit()

    paper_id, _, mode = payload_ref.rpartition(":")
    pdf = previous = text_id = None
    result = None
    code = None
    with session_factory() as db:
        paper = db.get(PaperRecord, paper_id)
        if paper is not None:
            text = _latest_parsing(db, paper_id)
            text_id = text.id if text is not None else None
            previous = _previous_markdown(db, artifact_store, paper)
            sha = paper.sha256
    if text_id is None:
        _finish(session_factory, job_id, worker_id, token, paper_id, None, "DOCPARSE_PDF_INVALID")
        return True

    stop, lost = threading.Event(), threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, name="docparse-heartbeat", daemon=True,
                            args=(session_factory, job_id, worker_id, token, heartbeat_seconds, stop, lost))
    beat.start()
    try:
        try:
            pdf = artifact_store.open_bytes(sha)
            result = service.parse(pdf, mode=mode, previous_markdown=previous,
                                   on_progress=_record_progress(session_factory, text_id),
                                   cancelled=lost.is_set)
        except DocParseError as e:
            code = e.code
        except Exception:
            code = _FALLBACK_CODES.get(mode, "DOCPARSE_REMOTE_FAILED")
    finally:
        stop.set()
        beat.join()
    if lost.is_set():
        return True  # lease was taken over; the new owner finishes the job
    _finish(session_factory, job_id, worker_id, token, paper_id, text_id, code, result, artifact_store)
    return True


def _finish(session_factory, job_id, worker_id, token, paper_id, text_id, code, result=None,
            artifact_store=None) -> None:
    address = artifact_store.put_bytes(result.markdown.encode("utf-8")) if result is not None else None
    with session_factory() as db:
        if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
            db.rollback()
            return
        paper = db.get(PaperRecord, paper_id)
        text = db.get(PaperTextRecord, text_id) if text_id else None
        if result is not None:
            text.status = "ready"
            text.text_sha256 = address.sha256
            text.backend = result.backend
            text.backend_version = result.backend_version
            text.page_count = result.page_count
            text.pages_parsed = result.pages_parsed
            text.chunk_map_json = list(result.chunks)
            text.warnings_json = list(result.warnings)
            text.error_json = None
            paper.status = "ready"
        else:
            if text is not None:
                text.status = "failed"
                text.error_json = {"code": code}
            if paper is not None:
                paper.status = "failed"
                paper.error_json = {"code": code}
        db.commit()


def start_docparse_worker(session_factory, artifact_store, service: DocParseService, *,
                          heartbeat_seconds: float = 20.0):
    stop = threading.Event()
    with session_factory() as db:
        JobRepository(db).requeue_expired(kinds=(PARSE_JOB_KIND,))
        db.commit()

    def run():
        while not stop.is_set():
            try:
                worked = process_next_parse_job(session_factory, artifact_store, service,
                                                heartbeat_seconds=heartbeat_seconds)
            except Exception:
                worked = False
            if not worked:
                stop.wait(0.2)

    thread = threading.Thread(target=run, name="docparse-worker", daemon=False)
    thread.start()
    return stop, thread


def stop_docparse_worker(stop, thread, timeout: float = _STOP_TIMEOUT_SECONDS) -> bool:
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=timeout)
    return not thread.is_alive() if thread is not None else True
