import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from p2r_api.main import create_app
from p2r_api.auth import IdentityService
from p2r_api.routes.projects import ProjectCreate, ProjectPatch, ProjectService
from p2r_persistence.models import UserRecord, AuditLogRecord


@pytest.fixture
def setup(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'auth.sqlite'}", allowed_origin="https://testserver")
    factory = app.state.runtime["session_factory"]
    with factory() as db:
        user = UserRecord(display_name="Test", can_create_project=True)
        db.add(user)
        db.commit()
        uid = user.id
        # The out-of-box product has no login endpoint: sessions are minted
        # through the trusted service seam and handed to the client as cookie.
        secret = IdentityService(db).create_session(uid)
    return app, factory, uid, secret


def test_default_headers_rejected(setup):
    app, _, _, _ = setup
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get('/api/v1/sessions', headers={'X-Actor-ID': 'dev-user'}).status_code == 401
        assert client.get('/api/v1/sessions').status_code == 401


def test_me_and_logout_with_seeded_session(setup):
    app, _, uid, secret = setup
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get('/api/v1/auth/me').status_code == 401
        client.cookies.set('p2r_session', secret)
        response = client.get('/api/v1/auth/me')
        assert response.status_code == 200
        info = response.json()['data']
        assert info['user']['id'] == uid
        assert secret not in response.text
        assert client.delete('/api/v1/auth/session').status_code == 403
        assert client.delete('/api/v1/auth/session', headers={'Origin': 'https://testserver', 'X-CSRF-Token': 'wrong'}).status_code == 403
        deleted = client.delete('/api/v1/auth/session', headers={'Origin': 'https://testserver', 'X-CSRF-Token': info['csrf_token']})
        assert deleted.status_code == 204
        assert client.get('/api/v1/auth/me').status_code == 401
        client.cookies.set('p2r_session', secret)
        assert client.get('/api/v1/auth/me').status_code == 401


def test_logout_requires_configured_origin(setup):
    app, _, _, secret = setup
    with TestClient(create_app(session_factory=app.state.runtime["session_factory"]), base_url="https://testserver") as client:
        # No allowed_origin configured: every state-changing cookie request is
        # rejected before any session lookup, so no origin can be spoofed.
        client.cookies.set('p2r_session', secret)
        assert client.delete('/api/v1/auth/session').status_code == 403


def test_project_validation_and_cas(setup):
    from p2r_orchestration.service import ServiceError
    _, factory, uid, _ = setup
    assert ProjectCreate(name='ok', description='').description == ''
    assert ProjectPatch(description='').description == ''
    for body in [{}, {'name': None}, {'description': None}, {'name': '  '}, {'name': 'ok', 'description': None}]:
        with pytest.raises(ValidationError):
            ProjectPatch(**body)
    with pytest.raises(ValidationError):
        ProjectCreate(name='  ')
    with factory() as first, factory() as second:
        row = ProjectService(first).create_project(uid, ProjectCreate(name='ok'))
        stale = second.get(type(row), row.id)
        assert stale.version == 1
        with pytest.raises(ServiceError) as exc:
            ProjectService(first).patch_project(uid, row.id, ProjectPatch(name='new'))
        assert exc.value.code == 'PRECONDITION_REQUIRED'
        changed = ProjectService(first).patch_project(uid, row.id, ProjectPatch(name='new'), expected_version=1)
        assert changed.version == 2
        with pytest.raises(ServiceError) as exc:
            ProjectService(second).patch_project(uid, row.id, ProjectPatch(name='lost'), expected_version=1)
        assert exc.value.code == 'REVISION_CONFLICT'
        assert len(list(first.scalars(select(AuditLogRecord).where(AuditLogRecord.resource_id == row.id)))) == 2
