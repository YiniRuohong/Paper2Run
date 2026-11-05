"""Service behavior tests use substitutes only at the unit boundary."""
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import zipfile

import pytest
from sqlalchemy import select

from p2r_orchestration.autonomous import AutonomousService
from p2r_orchestration.autonomous import evidence
from p2r_orchestration.autonomous.worker import dispatch
from p2r_orchestration.service import ServiceError
from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import UserRecord, ProjectRecord
from p2r_persistence.autonomous import AutonomousTaskRecord as Task


def configuration(project="project"):
    return {"schema_version": "paper2run.project.v1", "project": {"id": project, "workspace": "work"},
        "model": {"provider_profile": "test", "credential_ref": "credential-store:test"},
        "harness": {"adapter": "pi", "skills": []},
        "target": {"kind": "local", "profile": "local", "image": "paper2run-experiment:1"},
        "limits": {"wall_time_seconds": 120, "max_model_calls": 10, "max_model_tokens": 10000,
                   "storage_bytes": 10000000, "gpu_devices": []},
        "network": {"public_internet": False, "host_and_metadata_access": False},
        "experiment": {"objective": "test"}}


def protocol():
    return {"objective": "test", "mode": "exploratory", "method": "regression", "entrypoint": "main.py",
            "expected_artifacts": ["predictions.json", "labels.json"], "evaluation": {"adapter": "regression", "metric": "mae"},
            "budget": configuration()["limits"]}


class Registry:
    def snapshot(self, ids):
        return []


class Target:
    instances = []
    def __init__(self, config, state_dir):
        self.config, self.state_dir = config, state_dir
        self.files, self.jobs, self.stops = {}, {}, []
        self.closed = False
        Target.instances.append(self)
    def probe(self): return {"available": True}
    def prepare(self): return {"ready": True}
    def close(self): self.closed = True
    def write(self, path, content):
        self.files[path] = content.encode()
        return {"bytes": len(content.encode())}
    def collect(self, path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[path])
        return {"sha256": hashlib.sha256(self.files[path]).hexdigest()}
    def start(self, command, launch_id, timeout_seconds):
        key = hashlib.sha256(launch_id.encode()).hexdigest()
        self.jobs.setdefault(key, {"job_id": key, "state": "completed", "exit_code": 0})
        return self.jobs[key]
    def status(self, job_id): return self.jobs.get(job_id, {"job_id": job_id, "state": "unknown"})
    def logs(self, job_id, offset=0, limit=16000): return {"text": "observed", "next_offset": 8}
    def stop(self, job_id):
        self.stops.append(job_id)
        return {"job_id": job_id, "state": "stopped"}


class Bridge:
    def __init__(self): self.aborted = False
    def abort(self): self.aborted = True
    def steer(self, message): pass
    def run(self, config, prompt, callback):
        path = Path(config["session_dir"]) / "test.jsonl"
        path.write_text('{"type":"session"}\n')
        callback({"type": "ready", "session_file": str(path), "session_id": "test"})
        callback({"type": "tool_request", "id": "tool-1", "name": "execute", "arguments": {"command": "true"}})
        callback({"type": "result", "text": "Success!", "usage": {"calls": 1, "input_tokens": 5, "output_tokens": 5, "total_tokens": 10}})


@pytest.fixture
def service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as db:
        db.add(UserRecord(id="owner", display_name="Owner")); db.flush()
        db.add(ProjectRecord(id="project", name="Project", created_by="owner")); db.commit()
    svc = AutonomousService(factory, tmp_path / "control", target_factory=Target, bridge_factory=Bridge, registry=Registry())
    svc.profiles.put("owner", {"id": "test", "base_url": "http://localhost:1234", "model": "test", "api": "openai-completions", "api_key": "unit-secret-123456"})
    with svc.sessions() as db:
        svc.put_config(db, "project", configuration(), None); db.commit()
    yield svc
    svc.shutdown()


def create(service, **kwargs):
    with service.lock, service.sessions() as db:
        row = service.create_task(db, "project", "owner", {"objective": "test", "authorization": {"confirmed": True}, **kwargs})
        db.commit()
        return row.id


def wait(service, task_id):
    service.workers[task_id]["thread"].join(timeout=10)
    assert not service.workers[task_id]["thread"].is_alive()
    with service.sessions() as db:
        return service.row(db, task_id)


def test_claimed_success_without_files_is_incomplete(service):
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "incomplete"
    assert row.report_json["verdict"] == "not_comparable"
    assert row.usage_json["calls"] == 1
    with service.sessions() as db:
        first = service.events(db, task_id)
        second = service.events(db, task_id, first["next_cursor"])
        assert first["events"] and second["events"] == []
        assert len({e["sequence"] for e in first["events"]}) == len(first["events"])
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    with service.sessions() as db:
        with pytest.raises(IntegrityError, match="append-only"):
            db.execute(text("UPDATE autonomous_events SET type='tampered' WHERE task_id=:task"), {"task": task_id})


def test_recovery_never_launches_and_requires_reinjected_key(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; row.usage_json = {"calls": 7, "total_tokens": 50, "wall_time_seconds": 20}; db.commit()
    recovered = AutonomousService(service.sessions, service.root, registry=Registry())
    recovered.recover()
    with recovered.sessions() as db:
        row = recovered.row(db, task_id)
        assert row.state == "paused" and row.error_json["code"] == "recovery_required"
        assert row.usage_json["calls"] == 7
        with pytest.raises(ServiceError, match="credentials"):
            recovered.control(db, task_id, "resume")
    assert not recovered.workers


def test_env_reference_resolves_after_restart(service, monkeypatch):
    monkeypatch.setenv("P2R_UNIT_KEY", "environment-secret")
    from p2r_orchestration.autonomous.security import ProfileStore
    grants = [("owner", "test", "http://localhost", "openai-completions", "test", "env:P2R_UNIT_KEY")]
    service.profiles = ProfileStore(environment_grants=grants)
    service.profiles.put("owner", {"id": "test", "base_url": "http://localhost", "model": "test", "api": "openai-completions", "credential_ref": "env:P2R_UNIT_KEY"})
    task_id = create(service)
    recovered = AutonomousService(service.sessions, service.root, registry=Registry(), environment_grants=grants)
    recovered.recover()
    with recovered.sessions() as db:
        row = recovered.control(db, task_id, "resume")
        assert row.state == "queued"
        assert "environment-secret" not in json.dumps(recovered.public(row))


def test_protocol_change_blocks_tools_until_exact_approval(service):
    task_id = create(service, protocol=protocol())
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    target = Target({}, service.root)
    proposal = {**protocol(), "method": "changed method"}
    result = dispatch(service, target, task_id, "propose_protocol", {"protocol": proposal, "reason": "change"}, "p1")
    assert result["paused"]
    with pytest.raises(ServiceError):
        dispatch(service, target, task_id, "write", {"path": "x", "content": "x"}, "p2")
    with service.sessions() as db:
        with pytest.raises(ServiceError): service.control(db, task_id, "resume")
        row = service.control(db, task_id, "resume", "approve:" + result["pending_decision"]["id"])
        assert row.protocol_json["protocol"]["method"] == "changed method"


def test_artifacts_are_frozen_exported_and_hash_checked(service):
    task_id = create(service, protocol=protocol())
    target = Target({}, service.root)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    for name, content in [("predictions.json", '[{"id":"1","prediction":3}]'), ("labels.json", '[{"id":"1","label":2}]')]:
        target.write(name, content)
        dispatch(service, target, task_id, "register_artifact", {"path": name, "name": name, "kind": "result"}, name)
    result = dispatch(service, target, task_id, "evaluate", {}, "eval")
    assert result["metrics"]["mae"] == 1 and result["verdict"] == "not_comparable"
    archive = evidence.export_zip(service, task_id)
    verified = evidence.verify_export_zip(archive)
    assert "task.json" in verified["files"]
    assert len(verified["manifest_sha256"]) == 64
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
        assert all(hashlib.sha256(zipped.read(n)).hexdigest() == info["sha256"] for n, info in manifest.items())
        assert b"unit-secret-123456" not in b"".join(zipped.read(n) for n in zipped.namelist())
    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as original, zipfile.ZipFile(tampered, "w") as zipped:
        for name in original.namelist():
            content = original.read(name)
            if name == "task.json":
                content = bytes([content[0] ^ 1]) + content[1:]
            zipped.writestr(name, content)
    with pytest.raises(ValueError, match="checksum|SHA-256 mismatch"):
        evidence.verify_export_zip(tampered.getvalue())
    with service.sessions() as db:
        root = evidence.artifact_root(service, service.row(db, task_id))
    (root / "predictions.json").write_text("tampered")
    with pytest.raises(ServiceError, match="hash mismatch"): evidence.export_zip(service, task_id)


def test_secret_payload_redacted_and_artifact_refused(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"
        service.event(db, row, "sdk", {"api_key": "unit-secret-123456", "text": "unit-secret-123456"}); db.commit()
        assert "unit-secret-123456" not in json.dumps(service.events(db, task_id))
    target = Target({}, service.root)
    target.write("secret", "unit-secret-123456")
    with pytest.raises(ServiceError, match="credential"):
        evidence.register(service, target, task_id, {"path": "secret", "name": "secret", "kind": "other"})
    with pytest.raises(ServiceError): evidence.safe_name("../../host")


def test_launch_id_deduplicates_and_stop_is_owned(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    target = Target({}, service.root)
    arguments = {"command": "true", "launch_id": "same", "timeout_seconds": 10}
    one = dispatch(service, target, task_id, "background", arguments, "1")
    two = dispatch(service, target, task_id, "background", arguments, "2")
    assert one == two and len(target.jobs) == 1
    with pytest.raises(ServiceError): dispatch(service, target, task_id, "stop", {"job_id": "another-task"}, "3")
    with pytest.raises(ServiceError): dispatch(service, target, task_id, "background", {**arguments, "command": "other"}, "4")


class WaitingBridge(Bridge):
    ready = threading.Event()
    def run(self, config, prompt, callback):
        callback({"type": "event", "event": {"type": "turn_start"}})
        self.ready.set()
        while not self.aborted:
            time.sleep(.01)


def test_pause_and_shutdown_abort_sdk_but_retain_jobs(service):
    WaitingBridge.ready.clear()
    service.bridge_factory = WaitingBridge
    task_id = create(service)
    service.launch(task_id)
    assert WaitingBridge.ready.wait(5)
    target = service.workers[task_id]["target"]
    service.shutdown()
    row = wait(service, task_id)
    assert row.state == "paused" and row.error_json["code"] == "recovery_required"
    assert target.closed and target.stops == []
    assert row.usage_json["calls"] == 1


def test_stop_confirms_job_termination_and_does_not_stop_other_tasks(service):
    task_id = create(service)
    owned = "a" * 64
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"owned": {"job_id": owned, "command": "sleep 60", "timeout_seconds": 60}}
        row.state = "paused"; db.commit()
        service.control(db, task_id, "stop"); db.commit()
    service.finish_stop(task_id)
    with service.sessions() as db:
        assert service.row(db, task_id).state == "stopped"
    assert Target.instances[-1].stops == [owned]


def test_missing_stop_confirmation_remains_paused(service):
    class UnknownTarget(Target):
        def stop(self, job_id): return {"job_id": job_id, "state": "unknown"}
    service.target_factory = UnknownTarget
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.jobs_json = {"lost": {"job_id": "b" * 64}}; row.state = "paused"; db.commit()
        service.control(db, task_id, "stop"); db.commit()
    service.finish_stop(task_id)
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.state == "paused" and row.error_json["code"] == "stop_unconfirmed"


def test_cumulative_usage_survives_two_resume_cycles(service):
    task_id = create(service)
    service.launch(task_id)
    one = wait(service, task_id)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "paused"; db.commit()
        service.control(db, task_id, "resume"); db.commit()
    service.launch(task_id)
    two = wait(service, task_id)
    assert two.usage_json["calls"] == 2
    assert two.usage_json["total_tokens"] == 20
    assert two.usage_json["wall_time_seconds"] >= one.usage_json["wall_time_seconds"]


def test_prepare_command_does_not_satisfy_experiment_completion(service):
    task_id = create(service, protocol=protocol())
    target = Target({}, service.root)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "running"; db.commit()
    for name, kind, content in [("main.py", "code", "print('ok')"), ("env.json", "environment", "{}"),
            ("data.json", "data", "{}"), ("predictions.json", "result", '[{"id":"a","prediction":1}]'),
            ("labels.json", "result", '[{"id":"a","label":1}]')]:
        target.write(name, content)
        evidence.register(service, target, task_id, {"path": name, "name": name, "kind": kind})
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.jobs_json = {"prepare": {"job_id": "prepare", "command": "echo main.py", "protocol_sha256": row.protocol_json["sha256"]}}
        service.event(db, row, "tool_completed", {"name": "execute", "result": {"job_id": "prepare", "exit_code": 0}})
        db.flush()
        result = evidence.report(service, db, row)
        assert result["status"] == "incomplete" and not result["successful_execution_observed"]


def reference_task(service, **protocol_changes):
    source = "https://example.org/table-1"
    labels = '[{"id":"a","label":1}]'
    spec = {**protocol(), "mode": "comparable", "evidence": [source],
            "data_sources": ["dataset:v1"], "split_hashes": {"test": "a" * 64},
            "parameters": {"split": "test"}, "allowed_changes": ["parameters"],
            "evaluation": {"adapter": "accuracy", "reference_value": 1.0,
                           "tolerance": 0.0, "direction": "maximize"},
            "provenance": {"labels_sha256": hashlib.sha256(labels.encode()).hexdigest(),
                           "labels_independently_frozen": True, "code_sha256": "b" * 64,
                           "environment_sha256": "c" * 64}, **protocol_changes}
    refs = {"schema_version": "paper2run.reference_values.v1", "references": [
        {"metric": "accuracy", "value": 1.0, "tolerance": 0.0, "direction": "maximize",
         "source_url": source, "scope": "dataset:v1/test"}]}
    task_id = create(service, protocol=spec, reference_values=refs,
                     reference_scope="dataset:v1/test", reference_sources_confirmed=True)
    return task_id, spec, labels


def test_reference_report_and_export_follow_custody_gate(service):
    task_id, _, labels = reference_task(service)
    target = Target({}, service.root)
    for name, content in [("labels.json", labels), ("predictions.json", '[{"id":"a","prediction":1}]')]:
        target.write(name, content)
        evidence.register(service, target, task_id, {"path": name, "name": name, "kind": "result"})
    with service.sessions() as db:
        row = service.row(db, task_id)
        result = evidence.report(service, db, row)
        db.commit()
    assert result["evaluation"]["metrics"]["accuracy"] == 1.0
    assert result["evaluation"]["verdict"] == "not_comparable"
    assert result["verification"]["verdict"] == "not_comparable"
    assert result["verification"]["comparisons"][0]["within_tolerance"] is None
    assert any("custody" in reason for reason in result["verification"]["limitations"])
    archive = evidence.export_zip(service, task_id)
    evidence.verify_export_zip(archive)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        assert json.loads(zipped.read("verification.json")) == result["verification"]
        assert json.loads(zipped.read("reference_values.json")) == result["reference_values"]


@pytest.mark.parametrize("auto_approved", [True, False])
def test_protocol_change_invalidates_reference_scope_confirmation(service, auto_approved):
    task_id, spec, _ = reference_task(service)
    proposed = {**spec, "parameters": {"split": "validation"}} if auto_approved else {**spec, "method": "new method"}
    with service.sessions() as db:
        row = service.row(db, task_id)
        decision = service.propose(db, row, proposed, "changed scope")
        if not auto_approved:
            service.control(db, task_id, "resume", "approve:" + decision["pending_decision"]["id"])
        assert row.reference_sources_confirmed is False
        assert row.reference_values_json is not None  # retained as unconfirmed provenance
        db.commit()


def test_runtime_directory_has_single_owner(service):
    service.recover()
    other = AutonomousService(service.sessions, service.root, registry=Registry())
    with pytest.raises(ServiceError, match="another autonomous runtime"):
        other.recover()


def test_version_conflicts_and_control_workspace_separation(service):
    with service.sessions() as db:
        with pytest.raises(ServiceError): service.put_config(db, "project", configuration(), None)
        invalid = configuration(); invalid["project"]["workspace"] = str(service.root)
        with pytest.raises(ServiceError): service.put_config(db, "project", invalid, "1")
    task_id = create(service)
    with service.sessions() as db:
        target = service.make_target(service.row(db, task_id))
        assert not service.root.is_relative_to(Path(target.config["workspace"]))
        assert not Path(target.config["workspace"]).is_relative_to(service.root)


def test_resume_cannot_reset_exhausted_budget(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id); row.state = "paused"; row.usage_json = {"calls": 10}
        db.commit()
        with pytest.raises(ServiceError, match="budget"): service.control(db, task_id, "resume")


def test_new_task_has_harness_state(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.harness_json == {"version": 2, "repair_round": 0, "no_progress": 0,
                                    "fingerprint": None, "candidate": None, "reviews": []}
        assert row.outcome is None
        public = service.public(row)
        assert public["outcome"] is None and public["repair_round"] == 0
        assert public["candidate"] is None and public["reviews"] == []


def test_public_exposes_legacy_task_without_harness_state(service):
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.harness_json = None
        public = service.public(row)
        assert public["repair_round"] == 0 and public["candidate"] is None and public["reviews"] == []
