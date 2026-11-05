"""The single local runtime: fixed local actor, kept routers only, stable data dir."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from p2r_api.main import create_local_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.install import OfflineInstaller
from p2r_orchestration.docparse_service import DocParseService
from p2r_persistence.models import UserRecord

LOCAL = {"X-Actor-ID": "demo-user"}


def _docparse(tmp_path):
    root = tmp_path / "docparse"
    return DocParseService(root, installer=OfflineInstaller(root, platform=lambda: "darwin-arm64"))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_WEB_DIST", str(tmp_path / "no-web-dist"))
    return create_local_app(db_url=f"sqlite:///{tmp_path / 'local.sqlite3'}",
                            artifact_store=LocalArtifactStore(tmp_path / "store"),
                            docparse_service=_docparse(tmp_path))


def test_fresh_local_runtime_can_create_and_list_projects(app):
    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "P"},
                              headers={**LOCAL, "Idempotency-Key": "k1"})
        assert created.status_code == 200, created.text  # existing contract: ETag response, 200
        listed = client.get("/api/v1/projects", headers=LOCAL).json()["data"]
        assert [p["id"] for p in listed] == [created.json()["data"]["id"]]


def test_local_actor_is_fixed(app):
    with TestClient(app) as client:
        assert client.get("/api/v1/projects").status_code == 200  # header defaults to the local actor
        assert client.get("/api/v1/projects", headers={"X-Actor-ID": "someone-else"}).status_code == 404
        assert client.get("/api/v1/auth/me").status_code == 401  # no-login local mode


def test_existing_local_user_is_left_untouched(app):
    with app.state.runtime["session_factory"].begin() as db:
        db.get(UserRecord, "demo-user").display_name = "Renamed"
    with TestClient(app):
        pass
    with app.state.runtime["session_factory"]() as db:
        assert db.get(UserRecord, "demo-user").display_name == "Renamed"


LEGACY_PREFIXES = ("/api/v1/demo", "/api/v1/sessions", "/api/v1/experiments", "/api/v1/runs",
                   "/api/v1/spec-revisions", "/api/v1/reports", "/api/v1/artifacts", "/api/v1/operations",
                   "/api/v1/provider-sessions", "/api/v1/environment-checks", "/api/v1/ssh-verifications",
                   "/api/v1/attempts", "/api/v1/executions")


def test_only_kept_routers_are_mounted(app):
    paths = set(app.openapi()["paths"])
    legacy = sorted(p for p in paths if p.startswith(LEGACY_PREFIXES) or "codegen" in p or "extraction" in p)
    assert legacy == []
    for kept in ("/healthz", "/api/v1/auth/me", "/api/v1/projects", "/api/v1/projects/{project_id}/papers",
                 "/api/v1/projects/{project_id}/papers/text", "/api/v1/papers/{paper_id}",
                 "/api/v1/papers/{paper_id}/parse", "/api/v1/papers/{paper_id}/text",
                 "/api/v1/docparse/status", "/api/v1/autonomous/projects/{project_id}/tasks"):
        assert kept in paths, kept


def test_default_data_dir_stays_p2r_demo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("P2R_DEMO_DIR", raising=False)
    monkeypatch.setenv("P2R_WEB_DIST", str(tmp_path / "no-web-dist"))
    create_local_app(docparse_service=_docparse(tmp_path))
    assert (tmp_path / ".p2r-demo" / "demo.sqlite3").is_file()


def test_data_dir_env_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_DEMO_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("P2R_WEB_DIST", str(tmp_path / "no-web-dist"))
    create_local_app(docparse_service=_docparse(tmp_path))
    assert (tmp_path / "custom" / "demo.sqlite3").is_file()
