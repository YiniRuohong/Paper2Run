from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from p2r_persistence.models import AuditLogRecord, MembershipRecord

from .auth import ForbiddenError, IdentityService, ResourceNotFoundError, require_project_access


_VALID_ROLES = {"owner", "editor", "viewer"}


def member_set(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    role: str,
    actor_user_id: str,
) -> MembershipRecord:
    """Create or update a project membership without logging credentials."""
    if role not in _VALID_ROLES:
        raise ValueError("invalid project role")

    project, _ = require_project_access(
        db,
        actor_user_id,
        project_id,
        action="member_set",
    )
    if project.deletion_state != "active":
        raise ForbiddenError("project membership changes are disabled")

    membership = db.scalar(
        select(MembershipRecord).where(
            MembershipRecord.project_id == project_id,
            MembershipRecord.user_id == user_id,
        )
    )
    if membership is None:
        membership = MembershipRecord(
            project_id=project_id,
            user_id=user_id,
            role=role,
        )
        db.add(membership)
    else:
        membership.role = role

    db.add(
        AuditLogRecord(
            actor_user_id=actor_user_id,
            action="membership_set",
            resource_type="project_membership",
            resource_id=membership.id,
            details_json={
                "project_id": project_id,
                "user_id": user_id,
                "role": role,
            },
        )
    )
    db.commit()
    return membership
