"""Paper2Run arm: the real product API (create_local_app) with the same model, image and task text."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx

from scenarios import SCENARIOS, expected_artifacts, split_ids

ROOT = Path(__file__).resolve().parents[2]
TRANSIENT = {"model_error", "sdk_error", "bridge_disconnected", "recovery_required", "bridge_protocol_error",
             "stdin_eof", "invalid_session"}
SKILLS = ["background-training", "data-preparation", "experiment-debugging", "implementation-lookup",
          "paper-analysis", "research-environment", "research-report", "result-audit"]


def free_port(start=8031):
    for port in range(start, start + 100):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free loopback port")


class Server:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(P2R_DATABASE_URL="sqlite:///" + str(self.runtime / "p2r.sqlite3"), P2R_DEMO_DIR=str(self.runtime),
                   P2R_DOCPARSE_DIR=str(self.runtime / "docparse"), P2R_AUTONOMOUS_DIR=str(self.runtime / "autonomous"),
                   P2R_ALLOWED_ORIGINS=self.base)
        self.log = (self.runtime / "server.log").open("ab")
        self.process = subprocess.Popen([sys.executable, "-m", "uvicorn", "p2r_api.main:create_local_app", "--factory",
                                         "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
                                        cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=self.base, timeout=60, trust_env=False,
                                   headers={"Origin": self.base, "Sec-Fetch-Site": "same-origin"})
        for _ in range(180):
            if self.process.poll() is not None:
                raise RuntimeError("API exited during startup; see server.log")
            try:
                if self.client.get("/healthz").status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(.5)
        raise RuntimeError("API startup timeout")

    def __exit__(self, *exc):
        self.client.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()

    def api(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid4())  # same key on retry: the server deduplicates
        for attempt in range(6):
            try:
                response = self.client.request(method, "/api/v1" + path, headers=headers, **kwargs)
                break
            except httpx.TransportError:
                if attempt == 5:
                    raise
                time.sleep(2 * (attempt + 1))
        if response.is_error:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code} {response.text[:600]}")
        return response.json()["data"]

    def raw(self, path):
        response = self.client.get(path, timeout=300)
        response.raise_for_status()
        return response.content


def protocol_for(name: str, objective: str, wall_seconds: int, repair_rounds: int) -> dict:
    s = SCENARIOS[name]
    evaluation = ({"adapter": "regression", "metric": "rmse", "direction": "minimize"} if s["task"] == "regression"
                  else {"adapter": "accuracy", "metric": "accuracy", "direction": "maximize"})
    return {"schema_version": "paper2run.protocol.v1", "objective": objective, "mode": "exploratory",
            "evidence": [s["pdf_url"]], "data_sources": [s["loader"] + " (bundled with scikit-learn)"],
            "split_hashes": {}, "method": s["method"],
            "parameters": {**s["parameters"], "split": split_ids(s["rows"])},
            "entrypoint": "train.py", "expected_artifacts": expected_artifacts(),
            "evaluation": {**evaluation, "reference_value": None, "tolerance": None},
            "allowed_changes": ["implementation fixes within the frozen method, data and split", "dependency installation"],
            "budget": {"wall_time_seconds": wall_seconds, "storage_bytes": 512 * 1024 * 1024, "gpu_devices": [],
                       "max_repair_rounds": repair_rounds},
            "provenance": {"labels_sha256": None, "labels_independently_frozen": False,
                           "code_sha256": None, "environment_sha256": None}}


def run_p2r(server: Server, name: str, paper_text: str, spec: str, model: dict, out: Path,
            wall_seconds: int, repair_rounds: int = 2, max_resumes: int = 3) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    s = SCENARIOS[name]
    started = time.monotonic()
    stats = {"arm": "paper2run", "scenario": name, "resumes": 0, "error": None}
    project = server.api("POST", "/projects", json={"name": f"harness-compare {name}", "description": s["title"]})
    paper = server.api("POST", f"/projects/{project['id']}/papers/text", json={
        "text": paper_text, "source_processing_consent": True, "title_hint": s["title"]})
    paper = server.api("GET", f"/papers/{paper['paper_id']}")
    profile = f"glm-{name}-{uuid4().hex[:6]}"
    server.api("POST", "/autonomous/profiles", json={"id": profile, "base_url": model["base_url"], "model": model["id"],
               "api": model["api"], "auth_mode": "none", "context_window": model["context_window"],
               "max_tokens": model["max_tokens"]})
    # Objective: the identical task text; harness mechanics come from Paper2Run's own system prompt.
    objective = spec
    if len(objective) > 8000:
        raise ValueError("task spec exceeds the objective limit")
    limits = {"wall_time_seconds": wall_seconds, "storage_bytes": 512 * 1024 * 1024, "gpu_devices": [],
              "max_repair_rounds": repair_rounds}
    config = {"schema_version": "paper2run.project.v1", "project": {"id": project["id"], "workspace": "experiment"},
              "model": {"provider_profile": profile, "credential_ref": "profile:" + profile}, "specialists": [],
              "harness": {"adapter": "pi", "skills": SKILLS},
              "target": {"kind": "local", "profile": "local-docker", "allocation": "dedicated", "isolation": "required",
                         "image": "paper2run-experiment:1"},
              "limits": limits, "network": {"public_internet": True, "host_and_metadata_access": False},
              "experiment": {"objective": objective, "protocol_change_policy": "version_and_pause_if_outside_authorization"}}
    server.api("PUT", f"/autonomous/projects/{project['id']}/config", json={"config": config})
    protocol = protocol_for(name, objective, wall_seconds, repair_rounds)
    task = server.api("POST", f"/autonomous/projects/{project['id']}/tasks", json={
        "objective": objective, "paper_version_id": paper["paper_version_id"], "protocol": protocol,
        "authorization": {"confirmed": True}})
    stats["task_id"] = task["id"]
    deadline = started + wall_seconds + 900
    last_state = None
    while True:
        task = server.api("GET", f"/autonomous/tasks/{task['id']}")
        state = task["state"]
        if state != last_state:
            print(f"[p2r {name}] state={state} round={task.get('repair_round')} outcome={task.get('outcome')}", flush=True)
            last_state = state
        if state in ("completed", "incomplete", "stopped"):
            break
        if state == "paused":
            code = (task.get("error") or {}).get("code")
            decision = task.get("pending_decision")
            if decision:
                # Stand-in for the user's one-click approval: approve only when the proposal keeps the
                # supplied data, split and evaluation; otherwise reject so the frozen protocol stands.
                proposed = ((decision.get("proposed") or {}).get("protocol") or {})
                keeps = (proposed.get("parameters", {}).get("split") == protocol["parameters"]["split"]
                         and proposed.get("evaluation") == protocol["evaluation"]
                         and proposed.get("data_sources") == protocol["data_sources"]
                         and proposed.get("entrypoint") == protocol["entrypoint"])
                action = "approve" if keeps else "reject"
                stats.setdefault("decisions", []).append({"kind": decision.get("kind"), "action": action,
                    "changed": decision.get("changed") or (decision.get("proposed") or {}).get("changed"),
                    "reason": str(decision.get("reason") or "")[:300]})
                print(f"[p2r {name}] protocol decision -> {action}", flush=True)
                server.api("POST", f"/autonomous/tasks/{task['id']}/decisions/{decision['id']}",
                           json={"action": action, "note": None if keeps else
                                 "Keep the supplied data, split, entrypoint and evaluation unchanged."})
            elif code in TRANSIENT and stats["resumes"] < max_resumes:
                stats["resumes"] += 1
                print(f"[p2r {name}] auto-resume #{stats['resumes']} after {code}", flush=True)
                server.api("POST", f"/autonomous/tasks/{task['id']}/control", json={"action": "resume"})
            else:
                stats["error"] = task.get("error")
                server.api("POST", f"/autonomous/tasks/{task['id']}/control", json={"action": "stop"})
        if time.monotonic() > deadline:
            stats["error"] = {"code": "harness_compare_deadline"}
            server.api("POST", f"/autonomous/tasks/{task['id']}/control", json={"action": "stop"})
            deadline = float("inf")
        time.sleep(5)
    stats["wall_seconds"] = round(time.monotonic() - started, 1)
    stats.update(state=task["state"], outcome=task.get("outcome"), usage=task.get("usage"),
                 repair_round=task.get("repair_round"))
    (out / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        (out / "export.zip").write_bytes(server.raw(f"/api/v1/autonomous/tasks/{task['id']}/export"))
    except Exception as exc:
        stats["export_error"] = str(exc)[:300]
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    # Evidence is exported; release this task's container and network (Docker's subnet pool is finite).
    for record in (server.runtime / "autonomous" / "tasks" / task["id"] / "execution").glob("p2r-*.json"):
        subprocess.run(["docker", "rm", "-f", record.stem], capture_output=True)
        subprocess.run(["docker", "network", "rm", record.stem + "-net"], capture_output=True)
    return stats
