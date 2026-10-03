"""identity, projects, memberships, and audit"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("identity_users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("display_name", sa.String(255), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("can_create_project", sa.Boolean(), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_identity_user_status"))
    op.create_table("identity_invites", sa.Column("token_hash", sa.String(64), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False), sa.Column("expires_at", TS, nullable=False), sa.Column("consumed_at", TS), sa.Column("revoked_at", TS), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False))
    op.create_index("ix_identity_invites_user_id", "identity_invites", ["user_id"])
    op.create_table("auth_sessions", sa.Column("token_hash", sa.String(64), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False), sa.Column("expires_at", TS, nullable=False), sa.Column("idle_expires_at", TS, nullable=False), sa.Column("last_seen", TS, nullable=False), sa.Column("revoked_at", TS), sa.Column("csrf_token_hash", sa.String(64), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False))
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table("projects", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(120), nullable=False), sa.Column("description", sa.String(2000), nullable=False), sa.Column("created_by", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False), sa.Column("version", sa.BigInteger(), nullable=False), sa.Column("deletion_state", sa.String(16), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_project_name"), sa.CheckConstraint("length(description) <= 2000", name="ck_project_description"), sa.CheckConstraint("version >= 1", name="ck_project_version"), sa.CheckConstraint("deletion_state IN ('active', 'deleting', 'deleted')", name="ck_project_deletion_state"))
    op.create_table("project_memberships", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False), sa.Column("role", sa.String(16), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.UniqueConstraint("project_id", "user_id", name="uq_membership_project_user"), sa.CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_membership_role"))
    op.create_index("ix_project_memberships_project_id", "project_memberships", ["project_id"])
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])
    op.create_table("audit_logs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("identity_users.id")), sa.Column("action", sa.String(100), nullable=False), sa.Column("resource_type", sa.String(100), nullable=False), sa.Column("resource_id", sa.String(36)), sa.Column("details_json", sa.JSON(), nullable=False), sa.Column("created_at", TS, nullable=False))
    for table, constraints in {
        "identity_users": [("ck_identity_user_status", "status IN ('active', 'disabled')")],
        "projects": [
            ("ck_project_name", "length(name) BETWEEN 1 AND 120"),
            ("ck_project_description", "length(description) <= 2000"),
            ("ck_project_version", "version >= 1"),
            ("ck_project_deletion_state", "deletion_state IN ('active', 'deleting', 'deleted')"),
        ],
        "project_memberships": [("ck_membership_role", "role IN ('owner', 'editor', 'viewer')")],
    }.items():
        with op.batch_alter_table(table) as batch:
            for name, condition in constraints:
                batch.create_check_constraint(name, condition)
    _audit_guard()


def _audit_guard() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("CREATE TRIGGER p2r_0003_audit_update BEFORE UPDATE ON audit_logs BEGIN SELECT RAISE(ABORT, 'append-only table'); END")
        op.execute("CREATE TRIGGER p2r_0003_audit_delete BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT, 'append-only table'); END")
    elif bind.dialect.name == "postgresql":
        op.execute("CREATE OR REPLACE FUNCTION p2r_0003_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only table'; END; $$")
        op.execute("CREATE TRIGGER p2r_0003_audit_guard BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION p2r_0003_audit_guard()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS p2r_0003_audit_update"); op.execute("DROP TRIGGER IF EXISTS p2r_0003_audit_delete")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS p2r_0003_audit_guard ON audit_logs"); op.execute("DROP FUNCTION IF EXISTS p2r_0003_audit_guard()")
    for table in ("audit_logs", "project_memberships", "projects", "auth_sessions", "identity_invites", "identity_users"):
        op.drop_table(table)
