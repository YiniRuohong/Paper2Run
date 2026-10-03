"""remove the invite-code system (out-of-box product)

The product no longer gates access behind one-time invite tokens: the guided
runtime provisions its local actor at demo bootstrap and IdentityService
creates sessions directly for trusted callers. The identity_invites table is
dropped; sessions (auth_sessions) and role memberships are unchanged.

Downgrade recreates the 0003 table shape; existing auth_sessions rows are
unaffected because identity_invites is referenced by nothing.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.drop_index("ix_identity_invites_user_id", table_name="identity_invites")
    op.drop_table("identity_invites")


def downgrade() -> None:
    op.create_table(
        "identity_invites",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("consumed_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_identity_invites_user_id", "identity_invites", ["user_id"])
