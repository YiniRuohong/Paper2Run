from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import sessionmaker

from .job_repository import JobRepository


class UnknownJobKind(LookupError):
    pass


class WorkerService:
    """Dispatch durable jobs through an explicit, trusted handler registry."""

    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory
        self._handlers: dict[str, Callable[[str], None]] = {}

    def register(self, kind: str, handler: Callable[[str], None]) -> None:
        self._handlers[kind] = handler

    def run_once(self, *, worker_id: str = "worker", lease_seconds: int = 60) -> bool:
        with self.session_factory.begin() as session:
            job = JobRepository(session).claim(worker_id=worker_id, lease_seconds=lease_seconds)
            if job is None:
                return False
            job_id, kind, payload_ref, fencing_token = job.id, job.kind, job.payload_ref, job.fencing_token
        handler = self._handlers.get(kind)
        if handler is None:
            with self.session_factory.begin() as session:
                JobRepository(session).fail(job_id, worker_id=worker_id, fencing_token=fencing_token, error=f"unknown job kind: {kind}")
            raise UnknownJobKind(kind)
        try:
            handler(payload_ref)
        except Exception as exc:
            with self.session_factory.begin() as session:
                JobRepository(session).fail(job_id, worker_id=worker_id, fencing_token=fencing_token, error=str(exc))
            raise
        with self.session_factory.begin() as session:
            JobRepository(session).complete(job_id, worker_id=worker_id, fencing_token=fencing_token)
        return True
