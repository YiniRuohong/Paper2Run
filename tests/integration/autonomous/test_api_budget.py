"""Real SDK + Docker budget lifecycle, using a controlled loopback provider."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from .test_api import client, setup, config, write, BASE


@pytest.mark.parametrize("budget", ["calls", "tokens", "wall"])
def test_real_sdk_container_budget_stops_background_job(client, budget):
    if not shutil.which(os.getenv("P2R_NODE_BINARY", "node")) or not shutil.which("docker"):
        pytest.skip("real Node/Docker required")
    image = subprocess.run(["docker", "image", "inspect", "paper2run-experiment:1"], capture_output=True, timeout=20)
    if image.returncode:
        pytest.skip("build scripts/autonomous/build-image.sh first")
    root = Path(__file__).resolve().parents[3]
    if not (Path(os.getenv("P2R_HARNESS_DIR", root / "apps/harness")) / "node_modules").is_dir():
        pytest.skip("install pinned harness dependencies first")
    release = threading.Event()
    captured, running = [], []
    svc = client.app.state.runtime["autonomous_service"]
    task_id, target = None, None
    launch_id = "budget-owned-background"
    job_id = hashlib.sha256(launch_id.encode()).hexdigest()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            first = len(captured) == 1
            if not first:
                current = next(iter(svc.workers.values()))["target"]
                running.append(current.status(job_id))
                if budget == "wall":
                    release.wait(80)
                    return
            delta = {"role": "assistant"}
            if first:
                delta["tool_calls"] = [{"index": 0, "id": "start_background", "type": "function",
                    "function": {"name": "background", "arguments": json.dumps({
                        "command": "sleep 120", "launch_id": launch_id, "timeout_seconds": 120})}}]
            else:
                delta["content"] = "Budget boundary reached."
            chunks = [
                {"id": "budget", "object": "chat.completion.chunk", "model": "research-test",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "budget", "object": "chat.completion.chunk", "model": "research-test",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if first else "stop"}],
                 "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}},
            ]
            raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        setup(client, f"http://127.0.0.1:{server.server_port}/v1")
        settings = config()
        settings["limits"].update(wall_time_seconds=45 if budget == "wall" else 120,
                                  max_model_calls=2 if budget == "calls" else 10,
                                  max_model_tokens=80 if budget == "tokens" else 20000)
        changed = write(client, "PUT", "/projects/project/config", {"config": settings}, headers={"If-Match": "1"})
        assert changed.status_code == 200, changed.text
        created = write(client, "POST", "/projects/project/tasks", {
            "objective": "Start one background job and enforce its task budget", "authorization": {"confirmed": True}})
        assert created.status_code == 202, created.text
        task_id = created.json()["data"]["id"]
        thread = svc.workers[task_id]["thread"]
        thread.join(75)
        assert not thread.is_alive(), "budget worker did not exit"
        target = svc.workers[task_id]["target"]
        task = client.get(BASE + f"/tasks/{task_id}").json()["data"]
        assert task["state"] == "stopped", task
        assert task["error"]["code"] in ("BUDGET_EXHAUSTED", "budget_exceeded", "wall_time_exceeded"), task
        assert task["report"]["status"] == "incomplete"
        assert task["report"]["verdict"] == "not_comparable"
        if budget != "wall":
            assert task["report"]["model_narrative"] == "Budget boundary reached."
        assert len(captured) == 2
        assert running and running[0]["state"] == "running", running
        # Command timeout is capped to remaining task wall-time. Its guardian
        # may finish before control-plane cancellation; preserve that timeout
        # rather than relabelling an already terminal job as an explicit stop.
        allowed_terminal = {"stopped", "timeout"} if budget == "wall" else {"stopped"}
        assert target.status(job_id)["state"] in allowed_terminal
        bridge = svc.workers[task_id]["bridge"]
        assert bridge.aborted.is_set() and bridge.process.poll() is not None
        with svc.sessions() as db:
            row = svc.row(db, task_id)
            assert svc.cancel_requested(row)
            assert row.context_json["cancel_intent"]["reason"] == row.error_json
            assert row.usage_json["calls"] == 2
            if budget == "tokens":
                assert row.usage_json["total_tokens"] == 80
            if budget == "wall":
                assert row.usage_json["wall_time_seconds"] >= 45
        journal = client.get(BASE + f"/tasks/{task_id}/events").json()["data"]["events"]
        assert any(e["type"] == "budget_exhausted" for e in journal)
        exits = [e for e in journal if e["type"] == "worker_exited"]
        assert exits[-1]["payload"]["background_jobs_retained"] is False
        replay = write(client, "POST", f"/tasks/{task_id}/control", {"action": "stop"})
        assert replay.status_code == 200 and replay.json()["data"]["error"] == task["error"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        if task_id:
            svc.workers[task_id]["bridge"].abort()
            svc.workers[task_id]["thread"].join(8)
            target = svc.workers[task_id].get("target")
        if target and hasattr(target, "container"):
            subprocess.run(["docker", "rm", "-f", target.container], capture_output=True, timeout=20)
            subprocess.run(["docker", "network", "rm", target.container + "-net"], capture_output=True, timeout=20)
