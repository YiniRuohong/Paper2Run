"""durable worker jobs"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(255), nullable=False),
        sa.Column("payload_ref", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("leased_by", sa.String(255)),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_available_at", "jobs", ["available_at"])
    op.create_index("ix_jobs_lease_until", "jobs", ["lease_until"])


def downgrade() -> None:
    for name in ("ix_jobs_lease_until", "ix_jobs_available_at", "ix_jobs_status", "ix_jobs_kind"):
        op.drop_index(name, table_name="jobs")
    op.drop_table("jobs")
