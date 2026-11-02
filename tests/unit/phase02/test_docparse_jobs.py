from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.errors import DocParseError
from p2r_docparse.markers import ParseResult
from p2r_orchestration.docparse_jobs import (
    PARSE_JOB_KIND, enqueue_paper_parse, process_next_parse_job, start_docparse_worker,
    stop_docparse_worker,
)
from p2r_orchestration.docparse_service import DocParseService
from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import Job, PaperRecord, PaperTextRecord, ProjectRecord, UserRecord

PDF = b"%PDF-fake"


def _result(md="# Parsed\n", backend="online_agent", warnings=None):
    return ParseResult(markdown=md, backend=backend, backend_version="v1", page_count=3, pages_parsed=3,
                       chunks=[{"first_page": 1, "last_page": 3, "status": "done"}],
                       warnings=warnings or [])


class FakeInstaller:
    def __init__(self, state="not_installed", healthy=True):
        self._state = SimpleNamespace(state=state, tier="pipeline", version="4.1.3")
        self.healthy = healthy

    def state(self):
        return self._state

    def ensure_healthy(self):
        return self.healthy


class Env:
    def __init__(self, tmp_path, online=None, offline=None, installer=None):
        engine = create_engine(f"sqlite:///{tmp_path / 'p.sqlite3'}")
        init_db(engine)
        self.sf = create_session_factory(engine)
        self.store = LocalArtifactStore(tmp_path / "art")
        self.online_calls, self.offline_calls = [], []

        def on(pdf, **kw):
            self.online_calls.append(kw)
            return online(pdf, **kw) if online else _result()

        def off(pdf, **kw):
            self.offline_calls.append(kw)
            return offline(pdf, **kw) if offline else _result(backend="offline_mineru")

        self.service = DocParseService(tmp_path / "dp", installer=installer or FakeInstaller(),
                                       parse_online_fn=on, parse_offline_fn=off)
        self.service.settings.set_mode("online")  # queued online jobs need online to still be selected
        with self.sf() as db:
            user = UserRecord(display_name="u", can_create_project=True)
            db.add(user)
            db.flush()
            project = ProjectRecord(name="p", created_by=user.id)
            db.add(project)
            db.flush()
            digest = self.store.put_bytes(PDF).sha256
            paper = PaperRecord(project_id=project.id, sha256=digest, artifact_key="k", bytes=len(PDF),
                                page_count=3)
            db.add(paper)
            db.commit()
            self.paper_id = paper.id

    def enqueue(self, mode, key):
        with self.sf() as db:
            job = enqueue_paper_parse(db, paper_id=self.paper_id, mode=mode, idempotency_key=key)
            db.commit()
            return job.id

    def run(self, **kw):
        return process_next_parse_job(self.sf, self.store, self.service, **kw)

    def paper(self):
        with self.sf() as db:
            return db.get(PaperRecord, self.paper_id)

    def texts(self):
        with self.sf() as db:
            return list(db.scalars(select(PaperTextRecord).where(PaperTextRecord.paper_id == self.paper_id)
                                   .order_by(PaperTextRecord.created_at)))


def test_enqueue_records_parsing_state(tmp_path):
    env = Env(tmp_path)
    env.enqueue("online", "k1")
    assert env.paper().status == "parsing"
    (text,) = env.texts()
    assert (text.status, text.page_count, text.source_sha256) == ("parsing", 3, env.paper().sha256)


def test_online_parse_success(tmp_path):
    env = Env(tmp_path)
    env.enqueue("online", "k1")
    assert env.run() is True
    (text,) = env.texts()
    assert text.status == "ready" and env.paper().status == "ready"
    assert text.backend == "online_agent"
    assert env.store.open_bytes(text.text_sha256) == b"# Parsed\n"
    assert text.text_sha256 == hashlib.sha256(b"# Parsed\n").hexdigest()
    assert env.run() is False


def test_progress_written_to_chunk_map(tmp_path):
    seen = []
    env = Env(tmp_path)

    def online(pdf, on_progress=None, **kw):
        on_progress(1, 3)
        seen.append(env.texts()[0].chunk_map_json)
        return _result()

    env.service._parse_online = lambda pdf, **kw: online(pdf, **kw)
    env.enqueue("online", "k1")
    env.run()
    assert seen == [[{"status": "done"}, {"status": "pending"}, {"status": "pending"}]]


def test_partial_result_is_ready_with_warnings(tmp_path):
    warn = ["第 21–40 页解析失败，未包含"]
    env = Env(tmp_path, online=lambda pdf, **kw: _result(warnings=warn))
    env.enqueue("online", "k1")
    env.run()
    (text,) = env.texts()
    assert text.status == "ready" and text.warnings_json == warn


def test_offline_not_ready_never_falls_back(tmp_path):
    env = Env(tmp_path, installer=FakeInstaller("not_installed"))
    env.enqueue("offline", "k1")
    env.run()
    (text,) = env.texts()
    assert text.status == "failed" and text.error_json == {"code": "DOCPARSE_OFFLINE_NOT_READY"}
    assert env.paper().status == "failed"
    assert env.online_calls == [] and env.offline_calls == []


def test_offline_unhealthy_fails_not_ready(tmp_path):
    env = Env(tmp_path, installer=FakeInstaller("ready", healthy=False))
    env.enqueue("offline", "k1")
    env.run()
    assert env.texts()[0].error_json == {"code": "DOCPARSE_OFFLINE_NOT_READY"}
    assert env.offline_calls == []


def test_offline_ready_uses_tier_and_version(tmp_path):
    env = Env(tmp_path, installer=FakeInstaller("ready"))
    env.enqueue("offline", "k1")
    env.run()
    assert env.offline_calls[0]["tier"] == "pipeline" and env.offline_calls[0]["version"] == "4.1.3"
    assert env.texts()[0].backend == "offline_mineru"


def test_online_job_queued_then_mode_switched_uploads_nothing(tmp_path):
    """I6/R1: a queued online job must not upload once the user has moved to offline."""
    env = Env(tmp_path, installer=FakeInstaller("ready"))
    env.service.settings.set_mode("online")
    env.enqueue("online", "k1")
    env.service.settings.set_mode("offline")
    env.run()
    assert env.online_calls == [] and env.offline_calls == []
    (text,) = env.texts()
    assert text.status == "failed" and text.error_json == {"code": "DOCPARSE_MODE_CHANGED"}
    assert env.paper().status == "failed"
    env.enqueue("offline", "k2")
    env.run()
    assert env.online_calls == [] and len(env.offline_calls) == 1


def test_offline_job_keeps_running_offline_after_switch_to_online(tmp_path):
    env = Env(tmp_path, installer=FakeInstaller("ready"))
    env.service.settings.set_mode("offline")
    env.enqueue("offline", "k1")
    env.service.settings.set_mode("online")
    env.run()
    assert env.online_calls == [] and len(env.offline_calls) == 1
    assert env.paper().status == "ready"


def test_previous_markdown_only_reused_from_online_records(tmp_path):
    """S2: an offline result must never seed an online retry."""
    partial = "<!-- p2r:pages 1-3 -->\n# old"
    for backend, expected in (("offline_mineru", None), ("online_agent", partial)):
        (tmp_path / backend).mkdir()
        env = Env(tmp_path / backend)
        address = env.store.put_bytes(partial.encode())
        with env.sf() as db:
            db.add(PaperTextRecord(paper_id=env.paper_id, source_sha256=env.paper().sha256, page_count=3,
                                   status="ready", text_sha256=address.sha256, backend=backend,
                                   warnings_json=["w"]))
            db.commit()
        env.service.settings.set_mode("online")
        env.enqueue("online", "k1")
        env.run()
        assert env.online_calls[0]["previous_markdown"] == expected


def test_unexpected_exception_maps_to_fixed_code(tmp_path):
    def boom(pdf, **kw):
        raise RuntimeError("secret internal detail")

    env = Env(tmp_path, online=boom)
    env.enqueue("online", "k1")
    env.run()
    (text,) = env.texts()
    assert text.error_json == {"code": "DOCPARSE_REMOTE_FAILED"}
    with env.sf() as db:
        assert db.scalar(select(Job)).status == "failed"


def test_docparse_error_code_persisted(tmp_path):
    def bad(pdf, **kw):
        raise DocParseError("DOCPARSE_PDF_INVALID")

    env = Env(tmp_path, online=bad)
    env.enqueue("online", "k1")
    env.run()
    assert env.texts()[0].error_json == {"code": "DOCPARSE_PDF_INVALID"}


def test_expired_lease_requeued_on_start(tmp_path):
    env = Env(tmp_path)
    job_id = env.enqueue("online", "k1")
    with env.sf() as db:
        job = db.get(Job, job_id)
        job.status, job.leased_by = "running", "dead"
        job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=30)
        db.commit()
    stop, thread = start_docparse_worker(env.sf, env.store, env.service)
    try:
        for _ in range(100):
            if env.paper().status == "ready":
                break
            threading.Event().wait(0.05)
    finally:
        assert stop_docparse_worker(stop, thread, timeout=5.0)
    assert env.paper().status == "ready"


def test_retry_passes_previous_markdown(tmp_path):
    warn = ["partial"]
    env = Env(tmp_path, online=lambda pdf, **kw: _result(md="PARTIAL MD", warnings=warn))
    env.enqueue("online", "k1")
    env.run()
    env.enqueue("online", "k2")
    env.run()
    assert env.online_calls[0]["previous_markdown"] is None
    assert env.online_calls[1]["previous_markdown"] == "PARTIAL MD"


def test_complete_previous_not_reused(tmp_path):
    env = Env(tmp_path)
    env.enqueue("online", "k1")
    env.run()
    env.enqueue("online", "k2")
    env.run()
    assert env.online_calls[1]["previous_markdown"] is None


def test_heartbeat_extends_lease(tmp_path):
    beats = []
    original = JobRepository.heartbeat

    def spy(self, *a, **kw):
        beats.append(a)
        return original(self, *a, **kw)

    first_beat = threading.Event()

    def slow(pdf, **kw):
        assert first_beat.wait(5.0)
        return _result()

    env = Env(tmp_path, online=slow)
    JobRepository.heartbeat = lambda self, *a, **kw: (spy(self, *a, **kw), first_beat.set())[0]
    try:
        env.enqueue("online", "k1")
        env.run(heartbeat_seconds=0.05)
    finally:
        JobRepository.heartbeat = original
    assert beats and env.paper().status == "ready"


def test_duplicate_enqueue_collapses_and_ready_not_flipped(tmp_path):
    env = Env(tmp_path)
    first = env.enqueue("online", "k1")
    second = env.enqueue("online", "k2")
    assert first == second
    assert len(env.texts()) == 1
    assert env.run() is True and env.run() is False
    assert env.paper().status == "ready"
    assert env.texts()[0].status == "ready"


def test_enqueue_reused_key_after_finish_is_noop(tmp_path):
    env = Env(tmp_path)
    first = env.enqueue("online", "k1")
    assert env.run() is True
    again = env.enqueue("online", "k1")
    assert again == first
    assert env.paper().status == "ready"
    assert len(env.texts()) == 1 and env.texts()[0].status == "ready"
    assert env.run() is False


def test_noop_when_no_parsing_record(tmp_path):
    env = Env(tmp_path)
    job_id = env.enqueue("online", "k1")
    with env.sf() as db:
        text = db.scalar(select(PaperTextRecord))
        text.status = "ready"
        db.get(PaperRecord, env.paper_id).status = "ready"
        db.commit()
    assert env.run() is True
    assert env.online_calls == []
    paper = env.paper()
    assert paper.status == "ready" and paper.error_json is None
    assert env.texts()[0].status == "ready" and env.texts()[0].error_json is None
    with env.sf() as db:
        assert db.get(Job, job_id).status == "succeeded"


def test_put_bytes_failure_marks_failed(tmp_path):
    env = Env(tmp_path, installer=FakeInstaller("ready"))
    env.enqueue("offline", "k1")
    real = env.store.put_bytes

    def broken(data):
        raise OSError("disk full")

    env.store.put_bytes = broken
    assert env.run() is True
    env.store.put_bytes = real
    (text,) = env.texts()
    assert text.status == "failed" and text.error_json == {"code": "DOCPARSE_OFFLINE_FAILED"}
    paper = env.paper()
    assert paper.status == "failed" and paper.error_json == {"code": "DOCPARSE_OFFLINE_FAILED"}
    with env.sf() as db:
        assert db.scalar(select(Job)).status == "failed"


def test_lost_lease_job_retaken_by_periodic_recovery(tmp_path):
    env = Env(tmp_path)
    stop, thread = start_docparse_worker(env.sf, env.store, env.service, recover_seconds=0.05)
    try:
        job_id = env.enqueue("online", "k1")
        # Simulate a worker that died right after claiming: running with an expired lease.
        with env.sf() as db:
            job = db.get(Job, job_id)
            job.status, job.leased_by = "running", "dead"
            job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=30)
            db.commit()
        for _ in range(200):
            if env.paper().status == "ready":
                break
            threading.Event().wait(0.05)
    finally:
        assert stop_docparse_worker(stop, thread, timeout=5.0)
    assert env.paper().status == "ready"


def test_worker_tick_between_enqueue_and_commit_does_not_strand_paper(tmp_path, monkeypatch):
    """C1: a worker tick in the gap before the route commits must not complete the job as a no-op."""
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker
    from p2r_orchestration import docparse_jobs

    env = Env(tmp_path)
    fast = sessionmaker(sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'p.sqlite3'}",
                                                 connect_args={"timeout": 0.2}), expire_on_commit=False)
    real_enqueue = JobRepository.enqueue

    def enqueue_then_tick(self, **kw):
        job = real_enqueue(self, **kw)
        try:
            process_next_parse_job(fast, env.store, env.service)
        except Exception:
            pass  # a locked database means the worker could not see the half-written state
        return job

    monkeypatch.setattr(docparse_jobs.JobRepository, "enqueue", enqueue_then_tick)
    env.enqueue("online", "k1")
    monkeypatch.undo()
    assert env.paper().status == "parsing"
    with env.sf() as db:
        assert db.scalar(select(Job)).status == "queued"
    assert env.run() is True
    assert env.paper().status == "ready" and env.texts()[0].status == "ready"
    assert len(env.online_calls) == 1


def test_worker_stop_cancels_parse_and_requeues_job(tmp_path):
    """I1: quitting the app cancels the running parse and leaves the job queued, not failed."""
    stop = threading.Event()
    seen = {}

    def offline(pdf, cancelled=None, **kw):
        seen["before"] = cancelled()
        stop.set()
        seen["after"] = cancelled()
        raise DocParseError("DOCPARSE_CANCELLED")

    env = Env(tmp_path, offline=offline, installer=FakeInstaller("ready"))
    env.enqueue("offline", "k1")
    assert process_next_parse_job(env.sf, env.store, env.service, stop=stop) is True
    assert seen == {"before": False, "after": True}
    with env.sf() as db:
        job = db.scalar(select(Job))
        assert job.status == "queued" and job.leased_by is None
    assert env.paper().status == "parsing" and env.texts()[0].status == "parsing"
    stop.clear()
    env.service._parse_offline = lambda pdf, **kw: _result(backend="offline_mineru")
    assert env.run() is True
    assert env.paper().status == "ready"


def test_worker_stop_requeues_even_if_installer_gave_up(tmp_path):
    stop = threading.Event()

    class Cancelled(FakeInstaller):
        def ensure_healthy(self):
            stop.set()
            return False  # installer.cancel() made the health check bail out

    env = Env(tmp_path, installer=Cancelled("ready"))
    env.enqueue("offline", "k1")
    process_next_parse_job(env.sf, env.store, env.service, stop=stop)
    with env.sf() as db:
        assert db.scalar(select(Job)).status == "queued"
    assert env.paper().status == "parsing"
