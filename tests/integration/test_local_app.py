"""The single local runtime: fixed local actor, kept routers only, stable data dir."""
from __future__ import annotations

from uuid import UUID

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


def _local_app(tmp_path, **kwargs):
    return create_local_app(db_url=f"sqlite:///{tmp_path / 'local.sqlite3'}",
                            artifact_store=LocalArtifactStore(tmp_path / "store"),
                            docparse_service=_docparse(tmp_path), **kwargs)


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_WEB_DIST", str(tmp_path / "no-web-dist"))
    monkeypatch.delenv("P2R_ALLOWED_ORIGINS", raising=False)


@pytest.fixture
def app(tmp_path):
    return _local_app(tmp_path)


# Restored from tests/integration/phase01/test_bootstrap.py (deleted in c6bb0a6): the request-id
# middleware and /healthz are kept behaviour.
def test_healthz_has_server_generated_request_id_and_payload(app):
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "p2r-api"}
    UUID(response.headers["X-Request-ID"])


def test_client_request_id_is_not_trusted_or_reflected(app):
    forged = "client-controlled-secret"
    response = TestClient(app).get("/healthz", headers={"X-Request-ID": forged})
    assert response.headers["X-Request-ID"] != forged
    UUID(response.headers["X-Request-ID"])


def test_validation_errors_use_the_field_error_envelope(app):
    with TestClient(app) as client:
        response = client.post("/api/v1/projects", json={"name": 1}, headers={**LOCAL, "Idempotency-Key": "bad"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert [e["path"] for e in body["error"]["details"]["field_errors"]] == ["name"]
    UUID(body["meta"]["request_id"])


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


# The whole public surface, exactly. Every route the web client calls is here (api-client/*.ts,
# features/docparse), plus GET/PATCH /projects/{id}, DELETE /auth/session and GET /papers/{id}/content.
KEPT_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/api/v1/auth/me"), ("DELETE", "/api/v1/auth/session"),
    ("GET", "/api/v1/projects"), ("POST", "/api/v1/projects"),
    ("GET", "/api/v1/projects/{project_id}"), ("PATCH", "/api/v1/projects/{project_id}"), ("DELETE", "/api/v1/projects/{project_id}"),
    ("GET", "/api/v1/projects/{project_id}/papers"), ("POST", "/api/v1/projects/{project_id}/papers"),
    ("POST", "/api/v1/projects/{project_id}/papers/text"),
    ("GET", "/api/v1/papers/{paper_id}"), ("GET", "/api/v1/papers/{paper_id}/content"),
    ("POST", "/api/v1/papers/{paper_id}/parse"), ("GET", "/api/v1/papers/{paper_id}/text"),
    ("GET", "/api/v1/docparse/status"), ("PUT", "/api/v1/docparse/mode"),
    ("POST", "/api/v1/docparse/offline/install"), ("POST", "/api/v1/docparse/offline/cancel"),
    ("DELETE", "/api/v1/docparse/offline"),
    ("GET", "/api/v1/autonomous/capabilities"), ("POST", "/api/v1/autonomous/capabilities/import"),
    ("PUT", "/api/v1/autonomous/capabilities/{capability_id}"),
    ("GET", "/api/v1/autonomous/profiles"), ("POST", "/api/v1/autonomous/profiles"),
    ("GET", "/api/v1/autonomous/projects/{project_id}/config"),
    ("PUT", "/api/v1/autonomous/projects/{project_id}/config"),
    ("GET", "/api/v1/autonomous/projects/{project_id}/target/probe"),
    ("GET", "/api/v1/autonomous/projects/{project_id}/tasks"),
    ("POST", "/api/v1/autonomous/projects/{project_id}/tasks"),
    ("GET", "/api/v1/autonomous/tasks/{task_id}"), ("GET", "/api/v1/autonomous/tasks/{task_id}/events"),
    ("POST", "/api/v1/autonomous/tasks/{task_id}/control"),
    ("POST", "/api/v1/autonomous/tasks/{task_id}/decisions/{decision_id}"),
    ("GET", "/api/v1/autonomous/tasks/{task_id}/files"), ("GET", "/api/v1/autonomous/tasks/{task_id}/files/raw"),
    ("GET", "/api/v1/autonomous/tasks/{task_id}/export"),
}


def test_route_surface_is_exactly_the_kept_surface(app):
    mounted = {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}
    assert sorted(mounted - KEPT_ROUTES) == [], "unexpected (legacy?) routes"
    assert sorted(KEPT_ROUTES - mounted) == [], "kept routes missing"


PROFILE = {"id": "guard", "base_url": "http://127.0.0.1:1", "model": "m", "api": "openai-completions",
           "api_key": "guard-test-key"}


def _post_profile(client, key, origin=None):
    headers = {**LOCAL, "Idempotency-Key": key}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/v1/autonomous/profiles", json=PROFILE, headers=headers)


def test_autonomous_writes_accept_only_the_configured_loopback_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_ALLOWED_ORIGINS", "http://127.0.0.1:8010")
    with TestClient(_local_app(tmp_path)) as client:
        assert _post_profile(client, "ok", "http://127.0.0.1:8010").status_code != 403
        for key, origin in (("vite", "http://127.0.0.1:5173"), ("evil", "http://evil.example"),
                            ("default", "http://127.0.0.1:8000"), ("missing", None)):
            response = _post_profile(client, key, origin)
            assert response.status_code == 403, (origin, response.text)
            assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_explicit_origins_win_over_env_and_cover_both_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_ALLOWED_ORIGINS", "http://127.0.0.1:9999")
    app = _local_app(tmp_path, allowed_origins=("http://127.0.0.1:8011", "http://localhost:8011"))
    with TestClient(app) as client:
        assert _post_profile(client, "a", "http://127.0.0.1:8011").status_code != 403
        assert _post_profile(client, "b", "http://localhost:8011").status_code != 403
        assert _post_profile(client, "c", "http://127.0.0.1:9999").status_code == 403


def test_default_origin_is_unchanged_without_configuration(app):
    with TestClient(app) as client:
        assert _post_profile(client, "a", "http://127.0.0.1:8000").status_code != 403
        assert _post_profile(client, "b", "http://127.0.0.1:8010").status_code == 403


@pytest.mark.parametrize("bad", ["https://evil.example", "http://127.0.0.1", "http://0.0.0.0:8010"])
def test_invalid_origin_configuration_fails_at_startup(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("P2R_ALLOWED_ORIGINS", bad)
    with pytest.raises(ValueError, match="P2R_ALLOWED_ORIGINS"):
        _local_app(tmp_path)
    with pytest.raises(ValueError, match="P2R_ALLOWED_ORIGINS"):
        _local_app(tmp_path, allowed_origins=[bad])


def test_logout_origin_check_accepts_any_configured_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_ALLOWED_ORIGINS", "http://127.0.0.1:8010,http://localhost:8010")
    with TestClient(_local_app(tmp_path)) as client:
        # Passing the origin check leads to the session check (401); failing it is 403.
        assert client.delete("/api/v1/auth/session", headers={"Origin": "http://localhost:8010"}).status_code == 401
        assert client.delete("/api/v1/auth/session", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 403


def test_default_data_dir_stays_p2r_demo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("P2R_DEMO_DIR", raising=False)
    create_local_app(docparse_service=_docparse(tmp_path))
    assert (tmp_path / ".p2r-demo" / "demo.sqlite3").is_file()


def test_data_dir_env_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_DEMO_DIR", str(tmp_path / "custom"))
    create_local_app(docparse_service=_docparse(tmp_path))
    assert (tmp_path / "custom" / "demo.sqlite3").is_file()
