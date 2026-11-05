"""Read-only workbench file listing + raw preview for autonomous tasks."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from p2r_api.main import create_app
from p2r_api.auth import IdentityService, csrf_token
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.models import UserRecord, ProjectRecord, MembershipRecord

BASE = "/api/v1/autonomous"
ORIGIN = "http://localhost:8000"
SECRET = "integration-private-key"


def config():
    return {"schema_version": "paper2run.project.v1", "project": {"id": "project", "workspace": "workspace"},
        "model": {"provider_profile": "test", "credential_ref": "credential-store:test"},
        "harness": {"adapter": "pi", "skills": []}, "target": {"kind": "local", "profile": "local"},
        "limits": {"wall_time_seconds": 180, "max_model_calls": 12, "max_model_tokens": 20000,
            "storage_bytes": 10000000, "gpu_devices": []},
        "network": {"public_internet": False, "host_and_metadata_access": False},
        "experiment": {"objective": "regression smoke"}}


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


def write(client, method, path, payload):
    return client.request(method, BASE + path, json=payload, headers={"Idempotency-Key": str(uuid4())})


@pytest.fixture
def task(client):
    profile = {"id": "test", "base_url": "http://127.0.0.1:1", "model": "m", "api": "openai-completions", "api_key": SECRET}
    assert write(client, "POST", "/profiles", profile).status_code == 201
    assert write(client, "PUT", "/projects/project/config", {"config": config()}).status_code == 200
    svc = client.app.state.runtime["autonomous_service"]
    with svc.sessions() as db:
        row = svc.create_task(db, "project", "owner", {"objective": "files", "authorization": {"confirmed": True}})
        row.state = "paused"
        task_id = row.id
        workspace = Path(row.config_snapshot["project"]["workspace"]) / row.id
        artifacts = svc.task_root(row.id) / "artifacts"
        entries = []
        for version, name, data in ((None, "main.py", b"print('hi')\n"), ("f" * 64, "out/result.json", b'{"mae": 0.5}'),
                                    (None, "plot.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>")):
            path = artifacts / (version or "unversioned") / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            entries.append({"name": name, "kind": "result", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                            "protocol_sha256": version, "source_path": name})
        entries.append({"name": "../escape", "kind": "result", "sha256": "0" * 64, "bytes": 1, "protocol_sha256": None, "source_path": "x"})
        row.artifacts_json = entries
        db.commit()
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "train.py").write_text("import torch\n")
    (workspace / "notes.md").write_text("# 笔记\n")
    (workspace / "leak.txt").write_text("key=" + SECRET)
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]\n")
    outside = workspace.parent / "outside-secret.txt"
    outside.write_text("outside")
    os.symlink(outside, workspace / "link.txt")
    os.symlink(workspace.parent, workspace / "linkdir")
    return task_id, workspace, artifacts


def raw(client, task_id, source, path, **extra):
    return client.get(BASE + f"/tasks/{task_id}/files/raw", params={"source": source, "path": path, **extra})


def as_actor(client, actor):
    client.cookies.set("p2r_session", client.secrets[actor])
    client.headers["X-CSRF-Token"] = csrf_token(client.secrets[actor])


def test_listing_contains_artifacts_and_workspace(client, task):
    task_id, _, _ = task
    response = client.get(BASE + f"/tasks/{task_id}/files")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["workspace_available"] is True and data["truncated"] is False
    by_path = {item["path"]: item for item in data["items"]}
    assert set(by_path) == {"artifacts/unversioned/main.py", "artifacts/ffffffffffff/out/result.json",
                            "artifacts/unversioned/plot.svg", "workspace/src/train.py", "workspace/notes.md",
                            "workspace/leak.txt"}
    assert [item["path"] for item in data["items"]] == sorted(by_path)
    main = by_path["artifacts/unversioned/main.py"]
    assert main["source"] == "artifact" and main["media_type"] == "text/x-python" and main["sha256"]
    assert by_path["artifacts/ffffffffffff/out/result.json"]["protocol_sha256"] == "f" * 64
    notes = by_path["workspace/notes.md"]
    assert notes["source"] == "workspace" and notes["media_type"] == "text/markdown" and notes["sha256"] is None
    assert notes["bytes"] == len("# 笔记\n".encode()) and notes["modified_at"]
    assert client.get(BASE + f"/tasks/{task_id}/files?x=1").status_code == 422


def test_raw_reads_with_safety_headers(client, task):
    task_id, _, _ = task
    response = raw(client, task_id, "workspace", "workspace/notes.md")
    assert response.status_code == 200, response.text
    assert response.content == "# 笔记\n".encode()
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox;")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == "inline; filename*=UTF-8''" + quote("notes.md")
    nested = raw(client, task_id, "artifact", "artifacts/ffffffffffff/out/result.json")
    assert nested.status_code == 200 and nested.content == b'{"mae": 0.5}'
    assert nested.headers["content-type"] == "application/json; charset=utf-8"
    svg = raw(client, task_id, "artifact", "artifacts/unversioned/plot.svg")
    assert svg.status_code == 200 and svg.headers["content-type"] == "text/plain; charset=utf-8"


def test_symlinks_traversal_and_params_rejected(client, task):
    task_id, _, _ = task
    assert raw(client, task_id, "workspace", "workspace/link.txt").status_code == 404
    assert raw(client, task_id, "workspace", "workspace/linkdir/outside-secret.txt").status_code == 404
    assert raw(client, task_id, "workspace", "workspace/.git/config").status_code == 404
    assert raw(client, task_id, "workspace", "workspace/missing.txt").status_code == 404
    for bad in ("workspace/../outside-secret.txt", "/etc/passwd", "workspace\\notes.md", "workspace/a\x00b",
                "workspace//notes.md", "workspace/./notes.md"):
        response = raw(client, task_id, "workspace", bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert raw(client, task_id, "artifact", "artifacts/unversioned/../escape").status_code == 422
    assert raw(client, task_id, "bogus", "workspace/notes.md").status_code == 422
    assert raw(client, task_id, "workspace", "workspace/notes.md", extra="1").status_code == 422
    assert client.get(BASE + f"/tasks/{task_id}/files/raw?source=workspace").status_code == 422
    # Source and prefix must agree.
    assert raw(client, task_id, "artifact", "workspace/notes.md").status_code == 404


def test_artifact_hash_mismatch_and_secret_refused(client, task):
    task_id, _, artifacts = task
    (artifacts / "unversioned" / "main.py").write_bytes(b"tampered")
    response = raw(client, task_id, "artifact", "artifacts/unversioned/main.py")
    assert response.status_code == 409 and response.json()["error"]["code"] == "EVIDENCE_INTEGRITY"
    leak = raw(client, task_id, "workspace", "workspace/leak.txt")
    assert leak.status_code == 409 and leak.json()["error"]["code"] == "SECRET_IN_ARTIFACT"
    assert SECRET not in leak.text


def test_project_isolation(client, task):
    task_id, _, _ = task
    as_actor(client, "other")
    assert client.get(BASE + f"/tasks/{task_id}/files").status_code == 404
    assert raw(client, task_id, "workspace", "workspace/notes.md").status_code == 404
    as_actor(client, "viewer")
    assert client.get(BASE + f"/tasks/{task_id}/files").status_code == 200
    assert client.get(BASE + f"/tasks/{'0' * 36}/files").status_code == 404


def test_ssh_target_workspace_unavailable(client, task):
    task_id, _, _ = task
    svc = client.app.state.runtime["autonomous_service"]
    with svc.sessions() as db:
        row = svc.row(db, task_id)
        snapshot = dict(row.config_snapshot)
        snapshot["target"] = {**snapshot["target"], "kind": "ssh", "remote_workspace": "/remote"}
        row.config_snapshot = snapshot
        db.commit()
    data = client.get(BASE + f"/tasks/{task_id}/files").json()["data"]
    assert data["workspace_available"] is False
    assert all(item["source"] == "artifact" for item in data["items"])
    assert raw(client, task_id, "workspace", "workspace/notes.md").status_code == 404
