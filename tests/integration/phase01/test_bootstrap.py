from uuid import UUID

from fastapi.testclient import TestClient

from p2r_api.main import create_app


def test_healthz_has_server_generated_request_id_and_payload():
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "p2r-api"}
    UUID(response.headers["X-Request-ID"])


def test_client_request_id_is_not_trusted_or_reflected():
    forged = "client-controlled-secret"
    response = TestClient(create_app()).get("/healthz", headers={"X-Request-ID": forged})
    assert response.headers["X-Request-ID"] != forged
    UUID(response.headers["X-Request-ID"])


def test_openapi_exposes_public_routes_without_internal_or_kernel_routes():
    paths = set(create_app().openapi()["paths"])
    expected_public_paths = {
        "/healthz",
        "/api/v1/projects/{project_id}/sessions",
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/cells",
        "/api/v1/sessions/{session_id}/actions",
        "/api/v1/sessions/{session_id}/checkpoints",
        "/api/v1/sessions/{session_id}/events",
    }

    assert expected_public_paths <= paths
    assert all("/internal/" not in path and "/kernel/" not in path for path in paths)
