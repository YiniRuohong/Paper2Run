from __future__ import annotations

import threading
import time

from sqlalchemy import select

from p2r_docparse.errors import DocParseError
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import Job, PaperRecord, PaperTextRecord

from .docparse_service import DocParseService

PARSE_JOB_KIND = "paper_parse"
_LEASE_SECONDS = 60
_STOP_TIMEOUT_SECONDS = 2.0
_FALLBACK_CODES = {"online": "DOCPARSE_REMOTE_FAILED", "offline": "DOCPARSE_OFFLINE_FAILED"}


def enqueue_paper_parse(db, *, paper_id: str, mode: str, idempotency_key: str):
    """Queue a parse; the mode is locked into the payload at this moment.

    A paper with a queued or running parse job reuses that job instead of adding another.
    """
    paper = db.get(PaperRecord, paper_id)
    if paper is None:
        raise KeyError(paper_id)
    active = db.scalar(select(Job).where(
        Job.kind == PARSE_JOB_KIND, Job.status.in_(("queued", "running")),
        Job.payload_ref.like(f"{paper_id}:%"),
    ).order_by(Job.created_at))
    if active is not None:
        return active
    reused = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if reused is not None and reused.status not in ("queued", "running"):
        return reused  # reused key of a finished job: nothing to parse, leave state alone
    # Write the parsing state first: the enqueue SAVEPOINT then nests in a real transaction, so a
    # worker cannot claim the job before the text row and paper status are committed with it.
    if _latest_parsing(db, paper_id) is None:
        db.add(PaperTextRecord(paper_id=paper_id, source_sha256=paper.sha256, page_count=paper.page_count,
                               status="parsing"))
    paper.status = "parsing"
    db.flush()
    return JobRepository(db).enqueue(kind=PARSE_JOB_KIND, payload_ref=f"{paper_id}:{mode}",
                                     idempotency_key=idempotency_key)


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
    try:
        _run_claimed(session_factory, artifact_store, service, job_id=job_id, token=token,
                     paper_id=paper_id, mode=mode, worker_id=worker_id,
                     heartbeat_seconds=heartbeat_seconds)
    except Exception:
        _fail_cleanup(session_factory, job_id, worker_id, token, paper_id, mode)
    return True


def _run_claimed(session_factory, artifact_store, service, *, job_id, token, paper_id, mode,
                 worker_id, heartbeat_seconds) -> None:
    with session_factory() as db:
        paper = db.get(PaperRecord, paper_id)
        text = _latest_parsing(db, paper_id) if paper is not None else None
        if text is None:
            # Nothing to parse (already finished or paper gone): complete as a no-op.
            JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token)
            db.commit()
            return
        text_id, sha = text.id, paper.sha256
        previous = _previous_markdown(db, artifact_store, paper)

    stop, lost = threading.Event(), threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, name="docparse-heartbeat", daemon=True,
                            args=(session_factory, job_id, worker_id, token, heartbeat_seconds, stop, lost))
    beat.start()
    result = code = None
    try:
        try:
            pdf = artifact_store.open_bytes(sha)
            result = service.parse(pdf, mode=mode, previous_markdown=previous,
                                   on_progress=_record_progress(session_factory, text_id),
                                   cancelled=lost.is_set)
        except DocParseError as e:
            code = e.code
    finally:
        stop.set()
        beat.join()
    if lost.is_set():
        return  # lease taken over or expired; periodic recovery requeues it
    _finish(session_factory, job_id, worker_id, token, paper_id, text_id, code, result, artifact_store)


def _fail_cleanup(session_factory, job_id, worker_id, token, paper_id, mode) -> None:
    code = _FALLBACK_CODES.get(mode, "DOCPARSE_REMOTE_FAILED")
    try:
        with session_factory() as db:
            JobRepository(db).fail(job_id, worker_id=worker_id, fencing_token=token, error=code)
            paper = db.get(PaperRecord, paper_id)
            if paper is not None and paper.status == "parsing":
                text = _latest_parsing(db, paper_id)
                if text is not None:
                    text.status, text.error_json = "failed", {"code": code}
                paper.status, paper.error_json = "failed", {"code": code}
            db.commit()
    except Exception:
        pass  # periodic lease recovery is the backstop


def _finish(session_factory, job_id, worker_id, token, paper_id, text_id, code, result=None,
            artifact_store=None) -> None:
    address = artifact_store.put_bytes(result.markdown.encode("utf-8")) if result is not None else None
    with session_factory() as db:
        if not JobRepository(db).complete(job_id, worker_id=worker_id, fencing_token=token):
            db.rollback()
            return
        paper = db.get(PaperRecord, paper_id)
        text = db.get(PaperTextRecord, text_id)
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
            text.status = "failed"
            text.error_json = {"code": code}
            paper.status = "failed"
            paper.error_json = {"code": code}
        db.commit()


def _recover(session_factory) -> None:
    with session_factory() as db:
        JobRepository(db).requeue_expired(kinds=(PARSE_JOB_KIND,))
        db.commit()


def start_docparse_worker(session_factory, artifact_store, service: DocParseService, *,
                          heartbeat_seconds: float = 20.0, recover_seconds: float = 30.0):
    stop = threading.Event()
    _recover(session_factory)

    def run():
        last = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last >= recover_seconds:
                last = time.monotonic()
                try:
                    _recover(session_factory)
                except Exception:
                    pass
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
