"""Real Pi SDK + Docker lifecycle acceptance; only the HTTP model is scripted.

Evidence stays in .p2r-e2e-audit/lifecycle/<run-id>. No task state is forged.
The application lifecycle is exercised through TestClient's ASGI lifespan.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from p2r_api.auth import IdentityService, csrf_token
from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.autonomous import AutonomousTaskRecord as Task
from p2r_persistence.models import MembershipRecord, ProjectRecord, UserRecord
from tests.integration.autonomous.test_api import BASE, ORIGIN, config, write


def eventually(observe, predicate, *, seconds=70):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = observe()
        if predicate(last):
            return last
        time.sleep(.15)
    pytest.fail(f"timed out waiting for lifecycle observation: {last!r}")


def test_real_sdk_docker_survives_app_exit_and_explicit_resume(monkeypatch):
    repo = Path(__file__).resolve().parents[3]
    if not shutil.which(os.getenv("P2R_NODE_BINARY", "node")) or not shutil.which("docker"):
        pytest.skip("real Node/Docker required")
    image = subprocess.run(["docker", "image", "inspect", "paper2run-experiment:1"], capture_output=True, timeout=20)
    if image.returncode:
        pytest.skip("build scripts/autonomous/build-image.sh first")
    if not (Path(os.getenv("P2R_HARNESS_DIR", repo / "apps/harness")) / "node_modules").is_dir():
        pytest.skip("install pinned harness dependencies first")
    evidence = repo / ".p2r-e2e-audit/lifecycle" / uuid4().hex
    evidence.mkdir(parents=True)
    monkeypatch.setenv("P2R_AUTONOMOUS_DIR", str(evidence / "control"))
    db_url = f"sqlite:///{evidence / 'api.sqlite'}"
    store = LocalArtifactStore(evidence / "artifacts")
    launch_id = "lifecycle-single-training"
    job_id = hashlib.sha256(launch_id.encode()).hexdigest()
    script = ("import os,time\n"
              "with open('launches.txt','a') as f: f.write(str(os.getpid())+'\\n')\n"
              "for i in range(600):\n print('lifecycle-tick',i,os.getpid(),flush=True); time.sleep(.25)\n")
    command = "python3 -u -c " + shlex.quote(script)
    background = [("background", {"command": command, "launch_id": launch_id, "timeout_seconds": 150})]
    steps = {1: background, 3: background,
             4: [("status", {"job_id": job_id}), ("logs", {"job_id": job_id, "offset": 0, "limit": 16000})]}
    captured, services = [], []
    release = threading.Event()
    blocked = {2: threading.Event(), 5: threading.Event()}
    summary = {"provider": "scripted loopback HTTP SSE", "sdk": "installed Pi Node SDK", "execution": "real local Docker"}
    target = task_id = None

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            number = len(captured)
            if number in blocked:
                blocked[number].set()
                release.wait(timeout=90)
                return
            calls = steps.get(number, [])
            delta = {"role": "assistant", "tool_calls": [
                {"index": i, "id": f"lifecycle_{number}_{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(calls)]}
            chunks = [
                {"id": "lifecycle", "object": "chat.completion.chunk", "model": "research-test",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "lifecycle", "object": "chat.completion.chunk", "model": "research-test",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}},
            ]
            raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=server.serve_forever, daemon=True)
    provider_thread.start()
    profile = {"id": "test", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "research-test",
               "api": "openai-completions", "api_key": "lifecycle-local-only-key", "context_window": 4096, "max_tokens": 128}

    def new_app():
        app = create_app(db_url=db_url, allowed_origin=ORIGIN, secure_cookies=False, artifact_store=store)
        services.append(app.state.runtime["autonomous_service"])
        return app

    def authenticate(http, secret):
        http.cookies.set("p2r_session", secret)
        http.headers.update({"Origin": ORIGIN, "X-CSRF-Token": csrf_token(secret)})

    def task(http):
        response = http.get(BASE + f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def journal(http):
        return http.get(BASE + f"/tasks/{task_id}/events").json()["data"]["events"]

    try:
        app = new_app()
        svc = services[-1]
        with svc.sessions() as db:
            db.add(UserRecord(id="owner", display_name="owner")); db.flush()
            db.add(ProjectRecord(id="project", name="Lifecycle", created_by="owner")); db.flush()
            db.add(MembershipRecord(project_id="project", user_id="owner", role="owner")); db.flush()
            secret = IdentityService(db).create_session("owner")
            db.commit()
        with TestClient(app) as http:
            authenticate(http, secret)
            assert write(http, "POST", "/profiles", profile).status_code == 201
            settings = config()
            settings["limits"].update(wall_time_seconds=240, max_model_tokens=100000, max_model_calls=10)
            assert write(http, "PUT", "/projects/project/config", {"config": settings}).status_code == 200
            response = write(http, "POST", "/projects/project/tasks", {
                "objective": "Run bounded lifecycle training; reconcile existing launch before any new work.",
                "authorization": {"confirmed": True}})
            assert response.status_code == 202, response.text
            task_id = response.json()["data"]["id"]
            assert blocked[2].wait(70), task(http)
            worker = svc.workers[task_id]
            target = worker["target"]
            launch_events = [e for e in journal(http) if e["type"] == "tool_completed"]
            if launch_events and launch_events[0]["payload"]["is_error"]:
                inspected = subprocess.run(["docker", "inspect", target.container], capture_output=True, text=True, timeout=20)
                if inspected.returncode == 0:
                    summary["address_diagnostic"] = {
                        "saved": target._addresses(),
                        "current": target._current_addresses(json.loads(inspected.stdout)[0]),
                    }
            assert launch_events and not launch_events[0]["payload"]["is_error"], launch_events
            initial = eventually(lambda: target.status(job_id), lambda s: s["state"] == "running")
            logs_before = eventually(lambda: target.logs(job_id), lambda s: "lifecycle-tick" in s.get("stdout", ""))
            assert task(http)["state"] == "running"
            assert worker["thread"].is_alive() and worker["bridge"].process.poll() is None
            with svc.sessions() as db:
                row = db.get(Task, task_id)
                session_file = row.session_file
                assert set(row.jobs_json) == {launch_id}
            assert Path(session_file).is_file()
            original_session = Path(session_file).read_text()
            summary.update(task_id=task_id, container=target.container, initial=initial,
                           session_file=session_file, initial_sdk_pid=worker["bridge"].process.pid)
        # Actual ASGI lifespan exit calls AutonomousService.shutdown().
        assert not worker["thread"].is_alive()
        assert worker["bridge"].process.poll() is not None
        after_exit = target.status(job_id)
        assert after_exit["state"] == "running" and after_exit["pid"] == initial["pid"]
        logs_after = eventually(lambda: target.logs(job_id), lambda s: s["next_offset"] > logs_before["next_offset"])
        summary.update(after_exit=after_exit, logs_before=logs_before, logs_after_exit=logs_after,
                       initial_sdk_returncode=worker["bridge"].process.returncode)
        count = len(captured)
        app2 = new_app()
        restored = services[-1]
        with TestClient(app2) as http:
            authenticate(http, secret)
            recovered = task(http)
            assert recovered["state"] == "paused" and recovered["error"]["code"] == "recovery_required", recovered
            assert not restored.workers and len(captured) == count
            denied = write(http, "POST", f"/tasks/{task_id}/control", {"action": "resume"})
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "CREDENTIAL_REQUIRED", denied.text
            assert not restored.workers
            time.sleep(.3)
            assert len(captured) == count
            summary.update(recovered=recovered, credential_rejection=denied.json(), calls_before_resume=count)
            assert write(http, "POST", "/profiles", profile).status_code == 201
            assert len(captured) == count and not restored.workers
            resumed = write(http, "POST", f"/tasks/{task_id}/control", {"action": "resume"})
            assert resumed.status_code == 200, resumed.text
            assert blocked[5].wait(70), {"task": task(http), "events": journal(http)}
            target2 = restored.workers[task_id]["target"]
            assert target2.container == target.container
            current = target2.status(job_id)
            assert current["state"] == "running" and current["pid"] == initial["pid"]
            assert current["process_start"] == initial["process_start"]
            assert target2.read("launches.txt")["content"].splitlines() == [str(initial["pid"])]
            with restored.sessions() as db:
                row = db.get(Task, task_id)
                assert row.session_file == session_file and set(row.jobs_json) == {launch_id}
            transcript = Path(session_file).read_text()
            old_header = json.loads(original_session.splitlines()[0])
            assert json.loads(transcript.splitlines()[0])["id"] == old_header["id"]
            assert transcript.startswith(original_session)
            assert launch_id in json.dumps(captured[2]) and "existing_jobs" in json.dumps(captured[2])
            events = journal(http)
            assert any(e["type"] == "job_reconciled" and e["payload"]["launch_id"] == launch_id for e in events)
            completed = [e for e in events if e["type"] == "tool_completed"]
            assert len([e for e in completed if e["payload"]["name"] == "background"]) == 2
            assert all(not e["payload"]["is_error"] for e in completed), completed
            assert {"status", "logs"} <= {e["payload"]["name"] for e in completed}
            stopped = write(http, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="lifecycle-stop")
            assert stopped.status_code == 200 and stopped.json()["data"]["state"] == "stopped", stopped.text
            stopped_status = target2.status(job_id)
            assert stopped_status["state"] == "stopped", stopped_status
            replay = write(http, "POST", f"/tasks/{task_id}/control", {"action": "stop"}, key="lifecycle-stop")
            assert replay.status_code == 200 and replay.json()["data"] == stopped.json()["data"]
            resumed_worker = restored.workers[task_id]
            resumed_worker["thread"].join(timeout=8)
            assert not resumed_worker["thread"].is_alive()
            assert resumed_worker["bridge"].process.poll() is not None
            summary.update(resumed=current, stopped=stopped_status, session_id=old_header["id"],
                           calls_total=len(captured), events=journal(http), result="passed")
    finally:
        for service in services:
            service.shutdown()
        if task_id and services:
            with services[-1].sessions() as db:
                row = db.get(Task, task_id)
                summary["final_task"] = services[-1].public(row)
                summary["final_events"] = services[-1].events(db, task_id)["events"]
        release.set()
        server.shutdown(); server.server_close(); provider_thread.join(timeout=3)
        if target is None and task_id and services:
            target = services[0].workers.get(task_id, {}).get("target")
        if target is not None:
            # Only this uniquely scoped task's deterministic container/network.
            removed = subprocess.run(["docker", "rm", "-f", target.container], capture_output=True, text=True, timeout=20)
            network = subprocess.run(["docker", "network", "rm", target.container + "-net"], capture_output=True, text=True, timeout=20)
            absent = subprocess.run(["docker", "inspect", target.container], capture_output=True, timeout=20)
            summary["cleanup"] = {"container_rm": removed.returncode, "network_rm": network.returncode, "container_absent": absent.returncode != 0}
        summary["provider_requests"] = captured
        (evidence / "evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Lifecycle evidence: {evidence / 'evidence.json'}")
        if target is not None:
            assert removed.returncode == network.returncode == 0 and absent.returncode != 0, summary["cleanup"]
