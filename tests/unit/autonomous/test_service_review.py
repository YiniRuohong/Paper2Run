"""Regression cases from the independent backend review."""
from datetime import timedelta
import json
import threading

import pytest

from .test_service import service, create, Target, Registry, protocol, configuration, wait
from p2r_orchestration.autonomous import AutonomousService
from p2r_orchestration.autonomous.security import ProfileStore
from p2r_orchestration.autonomous.worker import dispatch
from p2r_orchestration.autonomous import evidence
from p2r_orchestration.errors import ServiceError
from p2r_persistence.models import utcnow


def test_environment_grant_requires_exact_actor_endpoint_and_identity(monkeypatch):
    monkeypatch.setenv("HOST_PRIVATE_TOKEN", "do-not-send")
    profile = {"id": "test", "base_url": "https://approved.example/v1", "api": "openai-completions",
               "model": "model", "credential_ref": "env:HOST_PRIVATE_TOKEN"}
    grant = ("owner", "test", profile["base_url"], profile["api"], profile["model"], profile["credential_ref"])
    untrusted = ProfileStore()
    with pytest.raises(ServiceError) as exc:
        untrusted.put("owner", profile)
    assert exc.value.code == "CREDENTIAL_REFERENCE_FORBIDDEN"
    trusted = ProfileStore(environment_grants=[grant])
    trusted.put("owner", profile)
    assert trusted.resolve("owner", "test")["api_key"] == "do-not-send"
    for actor, changed in [("other", {}), ("owner", {"base_url": "https://attacker.example"}),
                           ("owner", {"model": "other"}), ("owner", {"id": "other"})]:
        with pytest.raises(ServiceError):
            trusted.put(actor, {**profile, **changed})
        with pytest.raises(ServiceError):
            trusted.resolve(actor, changed.get("id", "test"), {**profile, **changed})
    assert "do-not-send" not in json.dumps(trusted.list("owner"))


@pytest.mark.parametrize("state", ["paused", "stopped", "completed", "running"])
def test_recover_settles_markers_in_every_state_once(service, state):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.state = state
        row.usage_json = {"calls": 2, "total_tokens": 100, "wall_time_seconds": 5,
                          "pending_token_reservation": 200,
                          "active_since": (utcnow() - timedelta(seconds=10)).isoformat()}
        db.commit()
    recovered = AutonomousService(service.sessions, service.root, registry=Registry())
    try:
        recovered.recover()
        with recovered.sessions() as db:
            row = recovered.row(db, task_id)
            settled = dict(row.usage_json)
            assert settled["total_tokens"] == 300 and settled["unverified_token_reservation"] == 200
            assert settled["wall_time_seconds"] >= 15
            assert "active_since" not in settled and "pending_token_reservation" not in settled
        recovered.recover()
        with recovered.sessions() as db:
            assert recovered.row(db, task_id).usage_json == settled
    finally:
        recovered.shutdown()


def test_late_start_after_unknown_stop_still_stops_owned_job(service):
    entered, released = threading.Event(), threading.Event()
    jobs, stops = {}, []
    class RacingTarget(Target):
        def start(self, command, launch_id, timeout_seconds):
            entered.set()
            assert released.wait(5)
            import hashlib
            job_id = hashlib.sha256(launch_id.encode()).hexdigest()
            jobs[job_id] = {"job_id": job_id, "state": "running"}
            return jobs[job_id]
        def status(self, job_id): return jobs.get(job_id, {"job_id": job_id, "state": "unknown"})
        def stop(self, job_id):
            stops.append(job_id)
            if job_id not in jobs:
                return {"job_id": job_id, "state": "unknown"}
            jobs[job_id] = {"job_id": job_id, "state": "stopped"}
            return jobs[job_id]
    service.target_factory = RacingTarget
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    target = RacingTarget({}, service.root)
    results, errors = [], []
    def execute():
        try:
            results.append(dispatch(service, target, task_id, "background",
                {"command": "sleep 30", "launch_id": "late", "timeout_seconds": 30}, "request"))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert entered.wait(3)
        with service.sessions() as db:
            service.control(db, task_id, "stop"); db.commit()
        service.finish_stop(task_id)
        with service.sessions() as db:
            row = service.row(db, task_id)
            assert row.state == "paused" and service.cancel_requested(row)
            with pytest.raises(ServiceError) as exc:
                service.control(db, task_id, "resume")
            assert exc.value.code == "CANCELLATION_PENDING"
    finally:
        released.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert results[0]["state"] == "stopped" and len(stops) == 2
    with service.sessions() as db:
        assert service.row(db, task_id).state == "stopped"
        assert service.cancel_requested(service.row(db, task_id))


@pytest.mark.parametrize("command,eligible", [
    ("python3 main.py ||true", False), ("python3 main.py;true", False),
    ("python3 main.py|cat", False), ("python3 main.py\ntrue", False),
    ("python3 main.py $(true)", False), ("python3 main.py >out", False),
    ("python3 main.py", True),
])
def test_completion_rejects_shell_status_masking(service, command, eligible):
    task_id = create(service, protocol=protocol())
    target = Target({}, service.root)
    for name, kind, content in [("main.py", "code", "raise SystemExit(7)"), ("env.json", "environment", "{}"),
            ("data.json", "data", "{}"), ("predictions.json", "result", '[{"id":"a","prediction":1}]'),
            ("labels.json", "result", '[{"id":"a","label":1}]')]:
        target.write(name, content)
        evidence.register(service, target, task_id, {"path": name, "name": name, "kind": kind})
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"job": {"job_id": "job", "command": command, "protocol_sha256": row.protocol_json["sha256"]}}
        service.event(db, row, "tool_completed", {"name": "execute", "result": {"job_id": "job", "exit_code": 0}})
        db.flush()
        result = evidence.report(service, db, row)
        assert result["successful_execution_observed"] is eligible
        assert (result["status"] != "incomplete") is eligible


def test_unavailable_execution_backend_pauses_without_breaking_runtime(service):
    def unsupported(*args):
        raise ImportError("POSIX isolation unavailable on this platform")
    service.target_factory = unsupported
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "paused" and row.error_json["code"] == "worker_error"
    assert "POSIX" in row.error_json["message"]
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"old": {"job_id": "a" * 64}}
        service.control(db, task_id, "stop"); db.commit()
    service.finish_stop(task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.state == "paused" and row.error_json["code"] == "stop_unconfirmed"
        assert service.cancel_requested(row)
