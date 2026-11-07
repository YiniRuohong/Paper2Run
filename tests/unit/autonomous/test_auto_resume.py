"""A demo must finish unattended: transient faults resume on their own, long waits never time the tool out.

Seen on a real Windows run: a 429 from the model service, a slow target probe and a
`sleep 300` inside execute each paused the task until someone pressed resume.
"""
from __future__ import annotations

import time

import pytest

from p2r_orchestration.autonomous import worker
from p2r_persistence.autonomous import AutonomousEventRecord as Event
from sqlalchemy import select

from .test_service import Bridge, Target, create, service  # noqa: F401


class Flaky(Bridge):
    """Fails the first `fail_times` runs with `error`, then behaves like the normal test bridge."""
    runs = 0
    fail_times = 1
    error = ("model_error", '429: {"message":"group requests-per-minute limit exceeded"}')

    def run(self, config, prompt, callback):
        Flaky.runs += 1
        if Flaky.runs <= Flaky.fail_times:
            exc = RuntimeError(Flaky.error[1])
            exc.code = Flaky.error[0]
            raise exc
        return super().run(config, prompt, callback)


@pytest.fixture
def flaky(service, monkeypatch):
    Flaky.runs, Flaky.fail_times = 0, 1
    Flaky.error = ("model_error", '429: {"message":"group requests-per-minute limit exceeded"}')
    service.bridge_factory = Flaky
    monkeypatch.setattr(worker, "AUTO_RESUME_DELAYS", (0.05,))
    return service


def events(service, task_id):
    with service.sessions() as db:
        return [e.type for e in db.scalars(select(Event).where(Event.task_id == task_id).order_by(Event.sequence))]


def settle(service, task_id, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        worker_state = service.workers.get(task_id)
        busy = worker_state and worker_state["thread"].is_alive()
        with service.sessions() as db:
            row = service.row(db, task_id)
            if not busy and row.state not in ("queued", "running"):
                time.sleep(0.3)  # an auto-resume may still be scheduled
                again = service.workers.get(task_id)
                if not (again and again["thread"].is_alive()):
                    with service.sessions() as db2:
                        row = service.row(db2, task_id)
                        if row.state not in ("queued", "running"):
                            return row
        time.sleep(0.05)
    raise AssertionError("task did not settle")


@pytest.mark.parametrize("code, message", [
    ("model_error", '429: {"message":"group requests-per-minute limit exceeded"}'),
    ("transport_timeout", "transport timed out; reconcile using the same launch_id"),
    ("tool_timeout", "tool_timeout"),
])
def test_transient_pause_resumes_by_itself(flaky, code, message):
    Flaky.error = (code, message)
    task_id = create(flaky)
    flaky.launch(task_id)
    row = settle(flaky, task_id)
    seen = events(flaky, task_id)
    assert seen.count("auto_resumed") == 1 and Flaky.runs >= 2
    assert seen.index("auto_resumed") < seen.index("session_ready")  # the resumed run did the work
    assert row.state != "paused" and (row.error_json or {}).get("code") != code


def test_authentication_failures_stay_paused(flaky):
    Flaky.error = ("model_error", "401: invalid api key")
    task_id = create(flaky)
    flaky.launch(task_id)
    row = settle(flaky, task_id)
    assert Flaky.runs == 1
    assert row.state == "paused" and "auto_resumed" not in events(flaky, task_id)


def test_auto_resume_is_bounded(flaky, monkeypatch):
    monkeypatch.setattr(worker, "AUTO_RESUME_LIMIT", 2)
    Flaky.fail_times = 99
    task_id = create(flaky)
    flaky.launch(task_id)
    row = settle(flaky, task_id)
    assert Flaky.runs == 3
    assert events(flaky, task_id).count("auto_resumed") == 2
    assert row.state == "paused" and row.error_json["code"] == "model_error"


def test_a_user_pause_cancels_the_scheduled_resume(flaky, monkeypatch):
    monkeypatch.setattr(worker, "AUTO_RESUME_DELAYS", (0.5,))
    task_id = create(flaky)
    flaky.launch(task_id)
    deadline = time.monotonic() + 5
    while "task_error" not in events(flaky, task_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    flaky.workers[task_id]["thread"].join(timeout=5)
    with flaky.lock, flaky.sessions() as db:
        flaky.control(db, task_id, "pause")
        db.commit()
    time.sleep(1.0)
    assert Flaky.runs == 1 and "auto_resumed" not in events(flaky, task_id)


def test_execute_returns_before_the_tool_response_limit(service, monkeypatch):
    class Slow(Target):
        def start(self, command, launch_id, timeout_seconds):
            job = super().start(command, launch_id, timeout_seconds)
            job["state"] = "running"
            return job

    monkeypatch.setattr(worker, "EXECUTE_WAIT_SECONDS", 0.3)
    task_id = create(service)
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        row.state = "running"
        db.commit()
    target = Slow({}, None)
    started = time.monotonic()
    result = worker.dispatch(service, target, task_id, "execute", {"command": "sleep 300", "timeout_seconds": 400}, "r1")
    assert time.monotonic() - started < 5
    assert result["state"] == "running" and result["still_running"] is True
    assert "status" in result["note"] and "logs" in result["note"]
    assert worker.EXECUTE_WAIT_SECONDS < 300  # the default stays under the bridge's 300 s limit


def test_default_execute_wait_is_below_bridge_limit():
    import importlib
    assert importlib.reload(worker).EXECUTE_WAIT_SECONDS <= 240
