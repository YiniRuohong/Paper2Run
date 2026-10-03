from datetime import timedelta
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import (
    AuditLogRecord, AuthSessionRecord, MembershipRecord,
    ProjectRecord, UserRecord, utcnow,
)
from p2r_api.auth import (
    IdentityService, SessionRequiredError, SessionExpiredError, ForbiddenError,
    ResourceNotFoundError, CsrfError, validate_origin, validate_csrf, csrf_token,
)
from p2r_api.routes.auth import session_info, set_session_cookie
from p2r_api.routes.projects import ProjectCreate, ProjectPatch, ProjectService
from p2r_api.admin import member_set


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as session:
        yield session
    engine.dispose()


def user(db, *, create=True):
    row = UserRecord(display_name="Researcher", can_create_project=create)
    db.add(row)
    db.commit()
    return row


def login(db, actor=None, now=None):
    actor = actor or user(db)
    secret = IdentityService(db).create_session(actor.id, now=now)
    return actor, secret


def test_session_is_hashed_and_dto_has_no_credentials(db):
    actor = user(db)
    identity = IdentityService(db)
    secret = identity.create_session(actor.id)
    row = db.scalar(select(AuthSessionRecord))
    assert row.token_hash != secret and len(row.token_hash) == 64
    auth = identity.get_current_session(secret)
    assert auth.token_hash != secret
    dto = session_info(db, auth, secret)
    dumped = json.dumps(dto.model_dump(mode="json"))
    assert secret not in dumped and auth.token_hash not in dumped
    assert "token_hash" not in dumped
    assert dto.csrf_token == csrf_token(secret)
    assert dto.permissions.can_create_project
    audit = str([r.__dict__ for r in db.scalars(select(AuditLogRecord))])
    assert secret not in audit and auth.token_hash not in audit


@pytest.mark.parametrize("reason", ["absolute", "idle", "revoked", "disabled"])
def test_session_invalidation(db, reason):
    now = utcnow()
    actor, secret = login(db, now=now)
    identity = IdentityService(db)
    auth = db.scalar(select(AuthSessionRecord))
    check_at = now
    if reason == "absolute":
        check_at = now + timedelta(hours=12)
    elif reason == "idle":
        check_at = now + timedelta(hours=2)
    elif reason == "revoked":
        identity.revoke_session(secret)
    else:
        actor.status = "disabled"
        db.commit()
    with pytest.raises((SessionRequiredError, SessionExpiredError)):
        identity.get_current_session(secret, now=check_at)


def test_idle_slides_without_extending_absolute(db):
    now = utcnow()
    _, secret = login(db, now=now)
    row = IdentityService(db).get_current_session(secret, now=now + timedelta(hours=1))
    assert row.last_seen == now + timedelta(hours=1)
    assert row.idle_expires_at == now + timedelta(hours=3)
    assert row.expires_at == now + timedelta(hours=12)


def test_origin_and_csrf():
    validate_origin("https://p2r.example", "https://p2r.example")
    validate_csrf("expected", "expected")
    for origin in [None, "null", "https://evil.example", "https://p2r.example.evil", "https://p2r.example/path"]:
        with pytest.raises(CsrfError):
            validate_origin(origin, "https://p2r.example")
    for header in [None, "", "wrong", "汉字"]:
        with pytest.raises(CsrfError):
            validate_csrf(header, "expected")


def test_cookie_defaults():
    from starlette.responses import Response
    response = Response()
    set_session_cookie(response, "opaque")
    cookie = response.headers["set-cookie"]
    assert all(part in cookie for part in ["HttpOnly", "Secure", "SameSite=lax", "Path=/"])
    response = Response()
    set_session_cookie(response, "opaque", secure=False)
    assert "Secure" not in response.headers["set-cookie"]


@pytest.mark.parametrize("role", ["owner", "editor", "viewer"])
def test_roles_and_cross_project(db, role):
    owner = user(db)
    member = user(db)
    outsider = user(db)
    projects = ProjectService(db)
    project = projects.create_project(owner.id, ProjectCreate(name="Study"))
    member_set(db, project_id=project.id, user_id=member.id, role=role, actor_user_id=owner.id)
    assert projects.require_project(member.id, project.id)[1].role == role
    for action in ["write", "execute", "export_create"]:
        if role == "viewer":
            with pytest.raises(ForbiddenError):
                projects.require_project(member.id, project.id, action=action)
        else:
            projects.require_project(member.id, project.id, action=action)
    if role != "owner":
        with pytest.raises(ForbiddenError):
            projects.require_project(member.id, project.id, action="delete")
    for project_id in [project.id, "missing"]:
        with pytest.raises(ResourceNotFoundError):
            projects.require_project(outsider.id, project_id, action="write")
    with pytest.raises(ForbiddenError):
        projects.require_project(member.id, project.id, action="undefined")


def test_project_owner_and_rollback(db, monkeypatch):
    actor = user(db)
    service = ProjectService(db)
    project = service.create_project(actor.id, ProjectCreate(name="Study"))
    member = db.scalar(select(MembershipRecord).where(MembershipRecord.project_id == project.id))
    assert member.user_id == actor.id and member.role == "owner"
    original = db.flush
    def fail_membership(*args, **kwargs):
        if any(isinstance(row, MembershipRecord) for row in db.new):
            raise RuntimeError("injected membership failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "flush", fail_membership)
    with pytest.raises(RuntimeError):
        service.create_project(actor.id, ProjectCreate(name="Must rollback"))
    monkeypatch.setattr(db, "flush", original)
    assert db.scalar(select(ProjectRecord).where(ProjectRecord.name == "Must rollback")) is None


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "a" * 121}, {"name": "valid", "description": "a" * 2001}])
def test_project_validation(body):
    with pytest.raises(ValidationError):
        ProjectCreate(**body)


def test_patch_and_create_permission(db):
    with pytest.raises(ValidationError):
        ProjectPatch()
    with pytest.raises(ValidationError):
        ProjectPatch(name=None)
    actor = user(db, create=False)
    with pytest.raises(ForbiddenError):
        ProjectService(db).create_project(actor.id, ProjectCreate(name="Study"))


def test_audit_is_append_only(db):
    actor = user(db)
    IdentityService(db).create_session(actor.id)
    for statement in ["UPDATE audit_logs SET action = 'changed'", "DELETE FROM audit_logs"]:
        with pytest.raises(DBAPIError):
            db.execute(text(statement))
        db.rollback()
    assert db.scalar(select(AuditLogRecord)) is not None
