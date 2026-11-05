"""Budget exhaustion cancels owned jobs; ordinary pause/exit retains them."""
import threading
from types import SimpleNamespace

import pytest

from .test_service import service, create, wait, Target, Bridge, WaitingBridge, Registry
from p2r_orchestration.autonomous import AutonomousService
from p2r_orchestration.autonomous import worker
from p2r_orchestration.errors import ServiceError


def seed_job(service, task_id):
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"owned": {"job_id": "a" * 64, "state": "running"}}
        db.commit()


def assert_cancelled(service, task_id, code, *, unknown=False):
    row = wait(service, task_id)
    assert row.state == ("paused" if unknown else "stopped")
    assert service.cancel_requested(row)
    reason = row.context_json["cancel_intent"]["reason"]
    assert reason["code"] == code
    if unknown:
        assert row.error_json["code"] == "stop_unconfirmed"
        assert row.error_json["cause"] == reason
        with service.sessions() as db:
            with pytest.raises(ServiceError) as exc:
                service.control(db, task_id, "resume")
            assert exc.value.code == "CANCELLATION_PENDING"
    else:
        assert row.error_json == reason
    assert service.workers[task_id]["bridge"].aborted
    assert "active_since" not in row.usage_json
    with service.sessions() as db:
        exits = [e for e in service.events(db, task_id)["events"] if e["type"] == "worker_exited"]
        assert exits[-1]["payload"]["background_jobs_retained"] is unknown
    return row


@pytest.mark.parametrize("code", ["budget_exceeded", "wall_time_exceeded"])
@pytest.mark.parametrize("unknown", [False, True])
def test_sdk_budget_error_cancels_owned_jobs_outside_lock(service, code, unknown):
    stops = []
    class BudgetTarget(Target):
        def stop(self, job_id):
            # A separate thread must acquire the service lock while remote stop runs.
            acquired = threading.Event()
            def acquire():
                with service.lock:
                    acquired.set()
            thread = threading.Thread(target=acquire)
            thread.start()
            assert acquired.wait(2), "remote stop held the service lock"
            thread.join(2)
            stops.append(job_id)
            return {"job_id": job_id, "state": "unknown" if unknown else "stopped"}
    class BudgetBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "event", "event": {"type": "turn_start"}})
            callback({"type": "event", "event": {"type": "message_end", "message": {
                "role": "assistant", "usage": {"input": 6, "output": 4, "totalTokens": 10}}}})
            callback({"type": "error", "code": code, "message": "budget exhausted"})
            # A late terminal packet must not replace the durable cancellation.
            callback({"type": "result", "text": "done", "usage": {"calls": 1, "total_tokens": 10}})
    service.target_factory, service.bridge_factory = BudgetTarget, BudgetBridge
    task_id = create(service)
    seed_job(service, task_id)
    service.launch(task_id)
    row = assert_cancelled(service, task_id, code, unknown=unknown)
    assert stops == ["a" * 64]
    assert row.usage_json["calls"] == 1 and row.usage_json["total_tokens"] == 10


@pytest.mark.parametrize("metric,value", [("calls", 10), ("total_tokens", 10000), ("wall_time_seconds", 120)])
def test_already_exhausted_budget_cancels_before_setup(service, metric, value):
    task_id = create(service)
    seed_job(service, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.usage_json = {metric: value}
        db.commit()
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert row.usage_json[metric] == value
    assert Target.instances[-1].stops == ["a" * 64]


@pytest.mark.parametrize("action", ["probe", "prepare"])
def test_setup_wall_budget_stops_before_next_action(service, monkeypatch, action):
    clock, actions = [0.0], []
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class SlowTarget(Target):
        def probe(self):
            actions.append("probe")
            if action == "probe": clock[0] = 121
            return {"available": True}
        def prepare(self):
            actions.append("prepare")
            if action == "prepare": clock[0] = 121
            return {"ready": True}
    class NoRunBridge(Bridge):
        def run(self, *args):
            actions.append("sdk")
    service.target_factory, service.bridge_factory = SlowTarget, NoRunBridge
    task_id = create(service)
    seed_job(service, task_id)
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert actions == (["probe"] if action == "probe" else ["probe", "prepare"])
    assert row.usage_json["wall_time_seconds"] == 121
    assert Target.instances[-1].stops == ["a" * 64]


def test_tool_dispatch_budget_failure_cancels_instead_of_returning_only_tool_error(service, monkeypatch):
    results, clock = [], [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class ToolBudgetBridge(Bridge):
        def run(self, config, prompt, callback):
            clock[0] = 121
            results.append(callback({"type": "tool_request", "id": "next", "name": "write",
                                     "arguments": {"path": "extra.py", "content": "unexpected"}}))
    service.bridge_factory = ToolBudgetBridge
    task_id = create(service)
    seed_job(service, task_id)
    service.launch(task_id)
    assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert results[0]["is_error"] and results[0]["result"]["code"] == "BUDGET_EXHAUSTED"
    assert not service.workers[task_id]["target"].files


@pytest.mark.parametrize("action", ["pause", "shutdown"])
def test_explicit_pause_and_exit_keep_owned_jobs(service, action):
    WaitingBridge.ready.clear()
    service.bridge_factory = WaitingBridge
    task_id = create(service)
    seed_job(service, task_id)
    before = len(Target.instances)
    service.launch(task_id)
    assert WaitingBridge.ready.wait(5)
    if action == "pause":
        with service.lock, service.sessions() as db:
            service.control(db, task_id, "pause")
            db.commit()
    else:
        service.shutdown()
    row = wait(service, task_id)
    assert row.state == "paused" and not service.cancel_requested(row)
    assert not any(t.stops for t in Target.instances[before:])


@pytest.mark.parametrize("metric,value", [("calls", 10), ("total_tokens", 10000)])
@pytest.mark.parametrize("kind", ["event", "result"])
def test_observed_budget_boundary_cancels_without_waiting_for_sdk_error(service, metric, value, kind):
    class BoundaryBridge(Bridge):
        def run(self, config, prompt, callback):
            if kind == "event":
                for _ in range(value if metric == "calls" else 1):
                    callback({"type": "event", "event": {"type": "turn_start"}})
                callback({"type": "event", "event": {"type": "message_end", "message": {
                    "role": "assistant", "usage": {"totalTokens": value if metric == "total_tokens" else 1}}}})
            else:
                callback({"type": "result", "text": "done", "usage": {metric: value}})
    service.bridge_factory = BoundaryBridge
    task_id = create(service)
    seed_job(service, task_id)
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert row.usage_json[metric] == value
    assert Target.instances[-1].stops == ["a" * 64]


def test_budget_cancel_without_jobs_preserves_reason(service):
    class BudgetBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "error", "code": "budget_exceeded", "message": "budget exhausted"})
    service.bridge_factory = BudgetBridge
    task_id = create(service)
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "budget_exceeded")
    assert row.jobs_json == {}


def test_interrupted_budget_cancel_recovers_cause_and_can_reconcile_stop(service):
    task_id = create(service)
    seed_job(service, task_id)
    reason = {"code": "budget_exceeded", "message": "budget exhausted"}
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.state = "stopping"
        row.context_json = {**row.context_json, "cancel_intent": {"requested": True, "reason": reason}}
        row.usage_json = {"calls": 10, "total_tokens": 52}
        db.commit()
    restored = AutonomousService(service.sessions, service.root, registry=Registry(), target_factory=Target)
    try:
        restored.recover()
        with restored.sessions() as db:
            row = restored.row(db, task_id)
            assert row.error_json["code"] == "stop_unconfirmed" and row.error_json["cause"] == reason
            assert restored.cancel_requested(row)
            with pytest.raises(ServiceError) as exc:
                restored.control(db, task_id, "resume")
            assert exc.value.code == "CANCELLATION_PENDING"
        restored.finish_stop(task_id)
        with restored.sessions() as db:
            row = restored.row(db, task_id)
            assert row.state == "stopped" and row.error_json == reason
            assert row.usage_json == {"calls": 10, "total_tokens": 52}
    finally:
        restored.shutdown()


def test_execute_polling_enforces_wall_budget_before_sdk_returns(service, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    class RunningTarget(Target):
        def start(self, command, launch_id, timeout_seconds):
            clock[0] = 121
            result = super().start(command, launch_id, timeout_seconds)
            return {**result, "state": "running"}
    class ExecuteBridge(Bridge):
        def run(self, config, prompt, callback):
            result = callback({"type": "tool_request", "id": "execute", "name": "execute",
                               "arguments": {"command": "sleep 120"}})
            assert result["is_error"] and self.aborted
    service.target_factory, service.bridge_factory = RunningTarget, ExecuteBridge
    task_id = create(service)
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert Target.instances[-1].stops == [next(iter(row.jobs_json.values()))["job_id"]]


def test_setup_exception_after_wall_budget_still_cancels(service, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class SlowFailTarget(Target):
        def prepare(self):
            clock[0] = 121
            raise OSError("prepare failed after deadline")
    service.target_factory = SlowFailTarget
    task_id = create(service)
    seed_job(service, task_id)
    service.launch(task_id)
    assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert Target.instances[-1].stops == ["a" * 64]


def test_exhausted_task_stops_without_requiring_model_credentials(service, monkeypatch):
    task_id = create(service)
    seed_job(service, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.usage_json = {"calls": 10}
        db.commit()
    def unavailable(*args):
        raise AssertionError("exhausted task must not resolve provider credentials")
    monkeypatch.setattr(service.profiles, "resolve", unavailable)
    service.launch(task_id)
    assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")


def test_budget_stop_backend_unavailable_preserves_cause(service):
    def unsupported(*args):
        raise ImportError("execution backend unavailable")
    task_id = create(service)
    seed_job(service, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.usage_json = {"calls": 10}
        db.commit()
    service.target_factory = unsupported
    service.launch(task_id)
    assert_cancelled(service, task_id, "BUDGET_EXHAUSTED", unknown=True)
    service.target_factory = Target
    service.finish_stop(task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.state == "stopped" and row.error_json["code"] == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize("metric", ["max_model_calls", "max_model_tokens"])
@pytest.mark.parametrize("terminal", ["result", "error"])
def test_budget_boundary_keeps_admitted_response_and_final_report(service, metric, terminal):
    narrative = "Final admitted response: observations are insufficient for a scientific result."
    class LastResponseBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "event", "event": {"type": "turn_start"}})
            callback({"type": "event", "event": {"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": narrative}],
                "usage": {"input": 6, "output": 4, "totalTokens": 10}}}})
            assert self.aborted
            result = callback({"type": "tool_request", "id": "forbidden", "name": "write",
                               "arguments": {"path": "extra.py", "content": "forbidden"}})
            assert result["is_error"]
            if terminal == "result":
                callback({"type": "result", "text": narrative, "usage": {"calls": 1, "total_tokens": 10}})
            else:
                callback({"type": "error", "code": "aborted", "message": "run aborted"})
    service.bridge_factory = LastResponseBridge
    task_id = create(service)
    seed_job(service, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.config_snapshot = {**row.config_snapshot, "limits": {**row.config_snapshot["limits"],
            metric: 1 if metric == "max_model_calls" else 10}}
        db.commit()
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED")
    assert row.report_json is not None
    assert row.report_json["model_narrative"] == narrative
    assert row.report_json["status"] == "incomplete" and row.report_json["verdict"] == "not_comparable"
    assert not service.workers[task_id]["target"].files


@pytest.mark.parametrize("unknown", [False, True])
def test_reconcile_stops_after_first_status_exhausts_wall_budget(service, monkeypatch, unknown):
    clock, actions = [0.0], []
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class SlowStatusTarget(Target):
        def status(self, job_id):
            actions.append("status:" + job_id[0])
            clock[0] += 121
            return {"job_id": job_id, "state": "running"}
        def stop(self, job_id):
            actions.append("stop:" + job_id[0])
            return {"job_id": job_id, "state": "unknown" if unknown else "stopped"}
    service.target_factory = SlowStatusTarget
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"one": {"job_id": "a" * 64}, "two": {"job_id": "b" * 64}}
        db.commit()
    service.launch(task_id)
    row = assert_cancelled(service, task_id, "BUDGET_EXHAUSTED", unknown=unknown)
    assert actions == ["status:a", "stop:a", "stop:b"]
    assert row.usage_json["wall_time_seconds"] == 121
