"""HTTP security + real Node SDK/Docker integration (scripted model provider).

No bridge substitutes: the end-to-end test invokes the installed SDK and actual
isolated target. Provider responses are controlled to keep the test deterministic;
this is not a claim of live-model or GPU acceptance.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from uuid import uuid4
import zipfile

from fastapi.testclient import TestClient
import pytest

from p2r_api.main import create_app
from p2r_api.auth import IdentityService, csrf_token
from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.autonomous import report_doc
from p2r_persistence.models import UserRecord, ProjectRecord, MembershipRecord, PaperRecord, PaperVersionRecord
from p2r_persistence.autonomous import AutonomousTaskRecord as Task
_spec = importlib.util.spec_from_file_location("autonomous_acceptance", Path(__file__).resolve().parents[3] / "tools/autonomous_acceptance.py")
_acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acceptance)
build_profile_payload = _acceptance.build_profile_payload

BASE = "/api/v1/autonomous"
ORIGIN = "http://localhost:8000"


def config():
    return {"schema_version": "paper2run.project.v1", "project": {"id": "project", "workspace": "workspace"},
        "model": {"provider_profile": "test", "credential_ref": "credential-store:test"},
        "harness": {"adapter": "pi", "skills": []}, "target": {"kind": "local", "profile": "local"},
        "limits": {"wall_time_seconds": 180, "max_model_calls": 12, "max_model_tokens": 20000,
            "storage_bytes": 10000000, "gpu_devices": []},
        "network": {"public_internet": False, "host_and_metadata_access": False},
        "experiment": {"objective": "regression smoke"}}


def protocol():
    return {"objective": "regression smoke", "mode": "exploratory", "method": "linear predictions",
        "entrypoint": "main.py", "evaluation": {"adapter": "regression", "metric": "mae"},
        "expected_artifacts": ["main.py", "environment.json", "data.json", "predictions.json", "labels.json"],
        "budget": config()["limits"]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_AUTONOMOUS_DIR", str(tmp_path / "control"))
    app = create_app(db_url=f"sqlite:///{tmp_path / 'api.sqlite'}", allowed_origin=ORIGIN, secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "paper-artifacts"))
    factory = app.state.runtime["session_factory"]
    with factory() as db:
        db.add_all([UserRecord(id=x, display_name=x) for x in ("owner", "other", "viewer")]); db.flush()
        db.add(ProjectRecord(id="project", name="Autonomous", created_by="owner")); db.flush()
        db.add_all([MembershipRecord(project_id="project", user_id="owner", role="owner"),
                    MembershipRecord(project_id="project", user_id="viewer", role="viewer")]); db.commit()
        secrets = {actor: IdentityService(db).create_session(actor) for actor in ("owner", "other", "viewer")}
    with TestClient(app) as http:
        http.cookies.set("p2r_session", secrets["owner"])
        http.headers.update({"Origin": ORIGIN, "X-CSRF-Token": csrf_token(secrets["owner"])})
        http.secrets = secrets
        yield http


def write(client, method, path, payload, key=None, **kwargs):
    headers = {"Idempotency-Key": key or str(uuid4()), **kwargs.pop("headers", {})}
    return client.request(method, BASE + path, json=payload, headers=headers, **kwargs)


def setup(client, endpoint="http://127.0.0.1:1"):
    profile = {"id": "test", "base_url": endpoint, "model": "research-test", "api": "openai-completions", "api_key": "integration-private-key"}
    assert write(client, "POST", "/profiles", profile).status_code == 201
    response = write(client, "PUT", "/projects/project/config", {"config": config()})
    assert response.status_code == 200, response.text


def test_loopback_anonymous_profile_requires_explicit_mode_and_is_admitted(client):
    profile = build_profile_payload("magpie", "http://127.0.0.1:3425/v1", "glm", "anthropic-messages", "", anonymous=True)
    assert "api_key" not in profile
    response = write(client, "POST", "/profiles", profile)
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["auth_mode"] == "none"
    assert body["secret_configured"] is False
    assert "api_key" not in body
    remote = {**profile, "id": "remote", "base_url": "http://203.0.113.10:3425/v1"}
    rejected = write(client, "POST", "/profiles", remote)
    assert rejected.status_code == 422


def test_membership_origin_csrf_version_and_profile_credentials(client):
    payload = {"config": config()}
    assert write(client, "PUT", "/projects/project/config", payload, headers={"Origin": "https://evil.example"}).status_code == 403
    assert write(client, "PUT", "/projects/project/config", payload, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    setup(client)
    assert write(client, "PUT", "/projects/project/config", payload).status_code == 412
    assert write(client, "PUT", "/projects/project/config", payload, headers={"If-Match": "1"}).status_code == 200
    profile = client.get(BASE + "/profiles").json()["data"][0]
    assert "api_key" not in profile and profile["secret_configured"] is True
    assert "integration-private-key" not in json.dumps(profile)
    for actor, status in (("other", 404), ("viewer", 403)):
        client.cookies.set("p2r_session", client.secrets[actor])
        client.headers["X-CSRF-Token"] = csrf_token(client.secrets[actor])
        result = write(client, "PUT", "/projects/project/config", payload, headers={"If-Match": "2"})
        assert result.status_code == status, result.text


def test_pdf_without_content_pauses_before_sdk_and_task_is_idempotent(client):
    setup(client)
    factory = client.app.state.runtime["session_factory"]
    with factory() as db:
        paper = PaperRecord(project_id="project", sha256="a" * 64, artifact_key="unused", bytes=8, page_count=1,
                            processing_consent=True, status="uploaded")
        db.add(paper); db.flush()
        version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=paper.sha256, artifact_key="unused", bytes=8, page_count=1)
        db.add(version); db.commit(); version_id = version.id
    payload = {"objective": "read uploaded paper", "paper_version_id": version_id, "authorization": {"confirmed": True}}
    first = write(client, "POST", "/projects/project/tasks", payload, key="same")
    replay = write(client, "POST", "/projects/project/tasks", payload, key="same")
    assert first.status_code == 202, first.text
    assert first.json()["data"] == replay.json()["data"]
    task = first.json()["data"]
    assert task["state"] == "paused" and task["error"]["code"] == "paper_content_required"
    assert not client.app.state.runtime["autonomous_service"].workers
    assert write(client, "POST", "/projects/project/tasks", {**payload, "objective": "different"}, key="same").status_code == 409
    assert write(client, "POST", f"/tasks/{task['id']}/control", {"action": "resume"}).status_code == 409
    client.cookies.set("p2r_session", client.secrets["other"])
    assert client.get(BASE + f"/tasks/{task['id']}").status_code == 404
    assert client.get(BASE + f"/tasks/{task['id']}/export").status_code == 404


def test_reference_values_survive_http_admission_report_and_export(client):
    from p2r_artifacts.archive_verify import verify_export_zip
    from p2r_orchestration.autonomous import evidence
    setup(client)
    svc = client.app.state.runtime["autonomous_service"]
    with svc.sessions() as db:
        paper = PaperRecord(project_id="project", sha256="d" * 64, artifact_key="unused", bytes=8, page_count=1,
                            processing_consent=True, status="uploaded")
        db.add(paper); db.flush()
        version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=paper.sha256, artifact_key="unused", bytes=8, page_count=1)
        db.add(version); db.commit(); version_id = version.id
    reference = {"schema_version": "paper2run.reference_values.v1", "references": [
        {"metric": "mae", "value": 0.1, "tolerance": 0.01, "direction": "minimize",
         "source_url": "https://example.org/paper/table1", "scope": "dataset:v1/test"}]}
    payload = {"objective": "verify reference metadata", "paper_version_id": version_id,
               "protocol": protocol(), "reference_values": reference,
               "reference_scope": "dataset:v1/test", "reference_sources_confirmed": True,
               "authorization": {"confirmed": True}}
    for invalid in ({**payload, "reference_sources_confirmed": None},
                    {**payload, "reference_scope": " "}, {**payload, "protocol": None}):
        rejected = write(client, "POST", "/projects/project/tasks", invalid)
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    admitted = write(client, "POST", "/projects/project/tasks", payload)
    assert admitted.status_code == 202, admitted.text
    task_id = admitted.json()["data"]["id"]
    assert admitted.json()["data"]["state"] == "paused"
    assert not svc.workers
    with svc.sessions() as db:
        row = svc.row(db, task_id)
        assert row.reference_values_json == reference
        assert row.reference_scope == "dataset:v1/test"
        assert row.reference_sources_confirmed is True
        report = evidence.report(svc, db, row)
        db.commit()
    assert report["verification"]["verdict"] == "not_comparable"
    exported = client.get(BASE + f"/tasks/{task_id}/export")
    assert exported.status_code == 200
    verify_export_zip(exported.content)
    with zipfile.ZipFile(io.BytesIO(exported.content)) as zipped:
        assert json.loads(zipped.read("reference_values.json")) == reference
        assert json.loads(zipped.read("verification.json")) == report["verification"]


def test_config_and_authorization_required(client):
    payload = {"objective": "test", "authorization": {"confirmed": True}}
    assert write(client, "POST", "/projects/project/tasks", payload).status_code == 409
    setup(client)
    assert write(client, "POST", "/projects/project/tasks", {"objective": "test"}).status_code == 422
    assert write(client, "POST", "/projects/project/tasks", {**payload, "authorization": {"confirmed": False}}).status_code == 422


def test_restart_http_recovery_requires_credentials_and_preserves_usage(client):
    setup(client)
    svc = client.app.state.runtime["autonomous_service"]
    # Persist a genuine task without scheduling it, simulating process loss
    # between committed admission and worker launch. No bridge substitute.
    with svc.sessions() as db:
        row = svc.create_task(db, "project", "owner", {"objective": "resume test", "authorization": {"confirmed": True}})
        row.state = "running"
        row.usage_json = {"calls": 3, "total_tokens": 100, "wall_time_seconds": 5}
        db.commit(); task_id = row.id
    from p2r_orchestration.autonomous import AutonomousService
    svc.shutdown()
    restored = AutonomousService(svc.sessions, svc.root, artifact_store=svc.artifact_store)
    restored.recover()
    client.app.state.runtime["autonomous_service"] = restored
    task = client.get(BASE + f"/tasks/{task_id}").json()["data"]
    assert task["state"] == "paused" and task["error"]["code"] == "recovery_required"
    assert task["usage"]["calls"] == 3
    result = write(client, "POST", f"/tasks/{task_id}/control", {"action": "resume"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "CREDENTIAL_REQUIRED"
    assert not restored.workers
    stopped = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="stop-once")
    replay = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="stop-once")
    assert stopped.status_code == 200 and replay.json()["data"] == stopped.json()["data"]
    assert stopped.json()["data"]["state"] == "stopped"
    restored.shutdown()


def test_http_environment_references_cannot_exfiltrate_host_secrets(client, monkeypatch):
    monkeypatch.setenv("P2R_HOST_PRIVATE", "host-secret-must-not-leave")
    payload = {"id": "malicious", "base_url": "https://attacker.example/v1", "model": "model",
               "api": "openai-completions", "credential_ref": "env:P2R_HOST_PRIVATE"}
    rejected = write(client, "POST", "/profiles", payload)
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "CREDENTIAL_REFERENCE_FORBIDDEN"
    assert client.get(BASE + "/profiles").json()["data"] == []
    setup(client)
    changed = config(); changed["model"]["credential_ref"] = "env:P2R_HOST_PRIVATE"
    assert write(client, "PUT", "/projects/project/config", {"config": changed}, headers={"If-Match": "1"}).status_code == 200
    rejected = write(client, "POST", "/projects/project/tasks", {"objective": "test", "authorization": {"confirmed": True}})
    assert rejected.status_code == 403 and not client.app.state.runtime["autonomous_service"].workers


def test_stop_idempotency_replay_reconciles_crash_after_admission(client, monkeypatch):
    setup(client)
    svc = client.app.state.runtime["autonomous_service"]
    with svc.sessions() as db:
        row = svc.create_task(db, "project", "owner", {"objective": "stop", "authorization": {"confirmed": True}})
        row.state = "paused"; db.commit(); task_id = row.id
    finish = svc.finish_stop
    # Simulate process loss after committed stop response, before reconciliation.
    monkeypatch.setattr(svc, "finish_stop", lambda task_id: None)
    first = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="interrupted-stop")
    assert first.json()["data"]["state"] == "stopping"
    monkeypatch.setattr(svc, "finish_stop", finish)
    replay = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="interrupted-stop")
    assert replay.status_code == 200 and replay.json()["data"]["state"] == "stopped"
    final = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="interrupted-stop")
    assert final.json()["data"] == replay.json()["data"]


def test_decision_endpoint_wiring_envelope_csrf_and_conflict(client, monkeypatch):
    setup(client)
    svc = client.app.state.runtime["autonomous_service"]
    with svc.sessions() as db:
        row = svc.create_task(db, "project", "owner", {"objective": "decide", "protocol": protocol(),
                                                       "authorization": {"confirmed": True}})
        decision = svc.propose(db, row, {**protocol(), "method": "changed"}, "change")["pending_decision"]
        db.commit(); task_id = row.id
    launched = []
    monkeypatch.setattr(svc, "launch", lambda task_id, message=None: launched.append((task_id, message)))
    path = f"/tasks/{task_id}/decisions/{decision['id']}"
    assert write(client, "POST", path, {"action": "reject"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert write(client, "POST", path, {"action": "defer"}).status_code == 422
    assert write(client, "POST", path, {"action": "reject", "note": "x" * 4001}).status_code == 422
    mismatch = write(client, "POST", f"/tasks/{task_id}/decisions/other", {"action": "approve"})
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "DECISION_MISMATCH"
    rejected = write(client, "POST", path, {"action": "reject", "note": "keep it"}, key="reject-once")
    assert rejected.status_code == 200, rejected.text
    task = rejected.json()["data"]
    assert task["id"] == task_id and task["state"] == "queued" and task["pending_decision"] is None
    assert launched == [(task_id, None)]
    replay = write(client, "POST", path, {"action": "reject", "note": "keep it"}, key="reject-once")
    assert replay.json()["data"] == task and len(launched) == 1
    with svc.sessions() as db:
        assert svc.row(db, task_id).protocol_json["protocol"]["method"] == "linear predictions"


def test_injected_database_roots_are_isolated_but_same_database_is_locked(tmp_path, monkeypatch):
    monkeypatch.delenv("P2R_AUTONOMOUS_DIR", raising=False)
    monkeypatch.delenv("P2R_DEMO_DIR", raising=False)
    first = create_app(db_url=f"sqlite:///{tmp_path / 'one.sqlite'}")
    second = create_app(db_url=f"sqlite:///{tmp_path / 'two.sqlite'}")
    duplicate = create_app(db_url=f"sqlite:///{tmp_path / 'one.sqlite'}")
    a, b = first.state.runtime["autonomous_service"], second.state.runtime["autonomous_service"]
    assert a.root != b.root
    from p2r_orchestration.errors import ServiceError
    with TestClient(first) as one, TestClient(second) as two:
        assert one.get("/healthz").status_code == two.get("/healthz").status_code == 200
        with pytest.raises(ServiceError, match="another autonomous runtime"):
            with TestClient(duplicate):
                pass


def test_real_sdk_container_api_to_report(client):
    node = os.getenv("P2R_NODE_BINARY", "node")
    if not shutil.which(node) or not shutil.which("docker"):
        pytest.skip("Node/Docker required for real integration")
    check = subprocess.run(["docker", "image", "inspect", "paper2run-experiment:1"], capture_output=True, timeout=20)
    if check.returncode:
        pytest.skip("build scripts/autonomous/build-image.sh before real integration")
    root = Path(__file__).resolve().parents[3]
    if not (Path(os.getenv("P2R_HARNESS_DIR", root / "apps/harness")) / "node_modules").exists():
        pytest.skip("install pinned harness dependencies before real integration")
    code = '''import json,sys
from pathlib import Path
Path("predictions.json").write_text(json.dumps([{"id":"a","prediction":3},{"id":"b","prediction":5}]))
Path("labels.json").write_text(json.dumps([{"id":"a","label":2},{"id":"b","label":5}]))
Path("environment.json").write_text(json.dumps({"python":sys.version}))
Path("data.json").write_text(json.dumps({"source":"controlled integration example; not scientific evidence"}))
print("actual execution complete")
'''
    steps = [
        [("background", {"command": "sleep 120", "launch_id": "integration-background", "timeout_seconds": 120})],
        [("write", {"path": "main.py", "content": code})],
        # Code is registered before the formal run so the run is attributable to it.
        [("register_artifact", {"path": "main.py", "name": "main.py", "kind": "code"})],
        [("execute", {"command": "python3 main.py", "timeout_seconds": 20})],
        [("register_artifact", {"path": name, "name": name, "kind": kind}) for name, kind in
            [("environment.json", "environment"), ("data.json", "data"), ("predictions.json", "result"), ("labels.json", "result")]],
        [("write", {"path": "plot.py", "content": "print('plot')"}),
         ("write", {"path": "fig.svg", "content": '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'})],
        [("register_artifact", {"path": "plot.py", "name": "plot.py", "kind": "analysis"}),
         ("register_artifact", {"path": "fig.svg", "name": "fig.svg", "kind": "figure", "caption": "controlled figure",
                                "derived_from": ["plot.py", "predictions.json"]})],
        [("submit_candidate", {"summary": "Actual controlled regression execution; no independent research evidence.",
                               "report": {**{name: f"{name}." for name in report_doc.SECTIONS},
                                          "results": "{{figure:fig.svg}}"}})],
        [],
        # Independent review session (same scripted provider, fresh SDK session).
        [("submit_review", {"verdict": "pass", "findings": []})],
        [],
    ]
    captured = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            calls = steps[min(len(captured) - 1, len(steps) - 1)]
            delta = {"role": "assistant"}
            if calls:
                delta["tool_calls"] = [{"index": i, "id": f"call_{len(captured)}_{i}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]
            else:
                delta["content"] = "Controlled experiment finished."
            chunks = [
                {"id": "test", "object": "chat.completion.chunk", "model": "research-test", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "test", "object": "chat.completion.chunk", "model": "research-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}},
            ]
            raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    svc = client.app.state.runtime["autonomous_service"]
    task_id, target = None, None
    try:
        setup(client, f"http://127.0.0.1:{server.server_port}/v1")
        # Actual text artifact is verified and reaches the SDK prompt through DB.
        store = client.app.state.runtime["artifact_store"]
        raw = "Controlled regression experiment: compare two observations. 测试正文".encode()
        address = store.put_bytes(raw)
        with svc.sessions() as db:
            paper = PaperRecord(project_id="project", title="Test paper", sha256=address.sha256, artifact_key=str(address.path), bytes=len(raw), page_count=1, processing_consent=True, status="uploaded")
            db.add(paper); db.flush()
            version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=address.sha256, artifact_key=str(address.path), bytes=len(raw), page_count=1, source_kind="text", media_type="text/plain")
            db.add(version); db.commit(); version_id = version.id
        response = write(client, "POST", "/projects/project/tasks", {"objective": "run controlled regression", "paper_version_id": version_id,
            "protocol": protocol(), "authorization": {"confirmed": True}})
        assert response.status_code == 202, response.text
        task_id = response.json()["data"]["id"]
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            task = client.get(BASE + f"/tasks/{task_id}").json()["data"]
            if task["state"] not in ("queued", "running"):
                break
            time.sleep(.2)
        assert task["state"] == "completed", json.dumps({"task": task, "events": client.get(BASE + f"/tasks/{task_id}/events").json()["data"]}, ensure_ascii=False)
        assert task["outcome"] == "verified" and task["report"]["outcome"] == "verified"
        assert task["candidate"]["review_status"] == "pass" and task["reviews"][0]["verdict"] == "pass"
        assert task["report"]["evaluation"]["metrics"]["mae"] == .5
        assert task["report"]["verdict"] == "not_comparable"
        assert task["usage"]["calls"] == 11
        assert "测试正文" in json.dumps(captured[0], ensure_ascii=False)
        journal = client.get(BASE + f"/tasks/{task_id}/events").json()["data"]
        assert any(e["type"] == "artifact_frozen" for e in journal["events"])
        assert not client.get(BASE + f"/tasks/{task_id}/events?after={journal['next_cursor']}").json()["data"]["events"]
        exported = client.get(BASE + f"/tasks/{task_id}/export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert "manifest.json" in archive.namelist()
            assert b"integration-private-key" not in b"".join(archive.read(n) for n in archive.namelist())
        stopped = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["state"] == "stopped", stopped.text
        target = svc.workers[task_id]["target"]
        actual_job = hashlib.sha256(b"integration-background").hexdigest()
        assert target.status(actual_job)["state"] == "stopped"
    finally:
        server.shutdown(); server.server_close()
        if task_id:
            svc.workers[task_id]["thread"].join(timeout=5)
            target = svc.workers[task_id].get("target")
        if target and hasattr(target, "container"):
            subprocess.run(["docker", "rm", "-f", target.container], capture_output=True, timeout=20)
            subprocess.run(["docker", "network", "rm", target.container + "-net"], capture_output=True, timeout=20)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site", "SAME-SITE"])
def test_writes_refuse_cross_and_same_site_case_insensitively(client, site):
    # Same rule as the target probe: another loopback port or sibling subdomain is same-site, not same-origin.
    refused = write(client, "PUT", "/projects/project/config", {"config": config()}, headers={"Sec-Fetch-Site": site})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "CSRF_FAILED"
    assert client.get(BASE + "/projects/project/config").status_code == 404, "nothing was written"


@pytest.mark.parametrize("site", ["same-origin", "Same-Origin", "none", None])
def test_writes_allow_same_origin_launcher_and_vite_proxy(client, site):
    # The launcher page and the Vite dev proxy both serve the page from the API's own origin,
    # so the browser sends same-origin; tools send no header at all.
    headers = {} if site is None else {"Sec-Fetch-Site": site}
    assert write(client, "PUT", "/projects/project/config", {"config": config()}, headers=headers).status_code == 200
