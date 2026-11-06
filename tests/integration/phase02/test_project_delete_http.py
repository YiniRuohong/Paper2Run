import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from p2r_api.main import create_app
from p2r_api.auth import IdentityService
from p2r_persistence.autonomous import AutonomousTaskRecord
from p2r_persistence.models import UserRecord, AuditLogRecord


@pytest.fixture
def setup(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'del.sqlite'}", allowed_origin="https://testserver")
    factory = app.state.runtime["session_factory"]
    with factory() as db:
        user = UserRecord(display_name="Test", can_create_project=True)
        db.add(user)
        db.commit()
        uid = user.id
        secret = IdentityService(db).create_session(uid)
    return app, factory, uid, secret


def _client(app, secret):
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("p2r_session", secret)
    csrf = client.get("/api/v1/auth/me").json()["data"]["csrf_token"]
    headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}
    return client, headers


def _create(client, headers, key="k1"):
    response = client.post("/api/v1/projects", json={"name": "draft"}, headers={**headers, "Idempotency-Key": key})
    assert response.status_code in (200, 201)
    return response.json()["data"]["id"]


def test_delete_draft_project_hides_it(setup):
    app, factory, _, secret = setup
    client, headers = _client(app, secret)
    with client:
        pid = _create(client, headers)
        assert client.delete(f"/api/v1/projects/{pid}", headers=headers).status_code == 204
        assert pid not in [p["id"] for p in client.get("/api/v1/projects").json()["data"]]
        assert client.get(f"/api/v1/projects/{pid}").status_code == 404
        assert client.delete(f"/api/v1/projects/{pid}", headers=headers).status_code == 404
    with factory() as db:
        logs = db.scalars(select(AuditLogRecord).where(AuditLogRecord.action == "project_deleted")).all()
        assert [log.resource_id for log in logs] == [pid]


def test_delete_refuses_project_with_tasks(setup):
    app, factory, uid, secret = setup
    client, headers = _client(app, secret)
    with client:
        pid = _create(client, headers)
        with factory() as db:
            db.add(AutonomousTaskRecord(project_id=pid, actor_id=uid, objective="o", config_snapshot={}))
            db.commit()
        response = client.delete(f"/api/v1/projects/{pid}", headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PROJECT_HAS_TASKS"
        assert pid in [p["id"] for p in client.get("/api/v1/projects").json()["data"]]


def test_delete_requires_csrf(setup):
    app, _, _, secret = setup
    client, headers = _client(app, secret)
    with client:
        pid = _create(client, headers)
        assert client.delete(f"/api/v1/projects/{pid}", headers={"Origin": "https://testserver"}).status_code == 403
        assert pid in [p["id"] for p in client.get("/api/v1/projects").json()["data"]]
