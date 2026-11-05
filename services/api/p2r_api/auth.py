from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from p2r_orchestration.errors import ServiceError
from p2r_persistence.models import (
    AuditLogRecord, AuthSessionRecord, MembershipRecord,
    ProjectRecord, UserRecord, utcnow,
)

SESSION_COOKIE = "p2r_session"
SESSION_MAX_AGE = timedelta(hours=12)
SESSION_IDLE = timedelta(hours=2)


class SessionRequiredError(ServiceError):
    status = 401
    code = "SESSION_REQUIRED"


class SessionExpiredError(SessionRequiredError):
    code = "SESSION_EXPIRED"


class ForbiddenError(ServiceError):
    status = 403
    code = "FORBIDDEN"


class CsrfError(ForbiddenError):
    code = "CSRF_FAILED"


class ResourceNotFoundError(ServiceError):
    status = 404
    code = "RESOURCE_NOT_FOUND"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(session_secret: str) -> str:
    return hmac.new(session_secret.encode(), b"p2r-csrf-v1", hashlib.sha256).hexdigest()


def validate_origin(request_origin: str | None, allowed_origin: str) -> None:
    try:
        parsed = urlsplit(allowed_origin)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or not request_origin or request_origin != allowed_origin:
        raise CsrfError("origin rejected")


def validate_csrf(header: str | None, expected: str) -> None:
    if not header or not expected or not hmac.compare_digest(header.encode(), expected.encode()):
        raise CsrfError("csrf token rejected")


class IdentityService:
    """Owns transaction commit/rollback; use a request-scoped DB session."""

    def __init__(self, db: Session):
        self.db = db

    def create_session(self, user_id: str, *, now: datetime | None = None) -> str:
        """Create a fresh authenticated session for an existing active user.

        The out-of-box product provisions its local user at demo bootstrap and
        never exposes a login endpoint; this service seam exists for trusted
        callers (tests, future controlled administration) only.
        """
        now = now or utcnow()
        user = self.db.get(UserRecord, user_id, populate_existing=True)
        if user is None or user.status != "active":
            raise ResourceNotFoundError("active user not found")
        session_secret = secrets.token_urlsafe(32)
        try:
            self.db.add(AuthSessionRecord(
                token_hash=_hash(session_secret), user_id=user.id,
                expires_at=now + SESSION_MAX_AGE, idle_expires_at=now + SESSION_IDLE,
                last_seen=now, csrf_token_hash=_hash(csrf_token(session_secret)),
            ))
            self.db.add(AuditLogRecord(actor_user_id=user.id, action="session_created",
                                      resource_type="user", resource_id=user.id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return session_secret

    def get_current_session(self, secret: str | None, *, now: datetime | None = None) -> AuthSessionRecord:
        if not secret:
            raise SessionRequiredError("session required")
        now = now or utcnow()
        row = self.db.get(AuthSessionRecord, _hash(secret), populate_existing=True)
        if row is None or row.revoked_at is not None:
            raise SessionRequiredError("session required")
        user = self.db.get(UserRecord, row.user_id, populate_existing=True)
        if user is None or user.status != "active":
            raise SessionRequiredError("session required")
        if row.expires_at <= now or row.idle_expires_at <= now:
            raise SessionExpiredError("session expired")
        new_seen = max(now, row.last_seen)
        new_idle = min(new_seen + SESSION_IDLE, row.expires_at)
        result = self.db.execute(update(AuthSessionRecord).where(
            AuthSessionRecord.token_hash == row.token_hash,
            AuthSessionRecord.revoked_at.is_(None),
            AuthSessionRecord.expires_at > now,
            AuthSessionRecord.idle_expires_at > now,
        ).values(last_seen=new_seen, idle_expires_at=new_idle))
        if result.rowcount != 1:
            self.db.rollback()
            raise SessionExpiredError("session expired")
        self.db.commit()
        self.db.refresh(row)
        return row

    def revoke_session(self, secret: str) -> None:
        row = self.db.get(AuthSessionRecord, _hash(secret), populate_existing=True)
        if row and row.revoked_at is None:
            row.revoked_at = utcnow()
            self.db.add(AuditLogRecord(actor_user_id=row.user_id, action="session_revoked",
                                      resource_type="user", resource_id=row.user_id))
            self.db.commit()


def role_allows(role: str, action: str) -> bool:
    permissions = {
        "owner": {"read", "write", "execute", "export_create", "delete", "member_set"},
        "editor": {"read", "write", "execute", "export_create"},
        "viewer": {"read"},
    }
    return action in permissions.get(role, set())


def require_project_access(db: Session, user_id: str, project_id: str, *, action: str = "read",
                           writes_enabled: bool = True) -> tuple[ProjectRecord, MembershipRecord]:
    user = db.get(UserRecord, user_id, populate_existing=True)
    if user is None or user.status != "active":
        raise SessionRequiredError("session required")
    project = db.get(ProjectRecord, project_id, populate_existing=True)
    membership = db.scalar(select(MembershipRecord).where(
        MembershipRecord.project_id == project_id, MembershipRecord.user_id == user_id,
    ).execution_options(populate_existing=True))
    if project is None or membership is None or project.deletion_state == "deleted":
        raise ResourceNotFoundError("resource not found")
    if not role_allows(membership.role, action):
        raise ForbiddenError("forbidden")
    if action != "read" and (not writes_enabled or project.deletion_state != "active"):
        raise ForbiddenError("project writes are disabled")
    return project, membership
