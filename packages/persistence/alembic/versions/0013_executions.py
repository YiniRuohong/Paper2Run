"""add execution records

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("runner_id", sa.String(length=255), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("handle", sa.String(length=255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id", "run_id"], ["attempts.id", "attempts.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_executions_job"),
        sa.UniqueConstraint("handle", name="uq_executions_handle"),
    )
    op.create_index("ix_executions_run_id", "executions", ["run_id"])
    op.create_index("ix_executions_attempt_id", "executions", ["attempt_id"])
    op.create_index("ix_executions_status", "executions", ["status"])


def downgrade():
    op.drop_index("ix_executions_status", table_name="executions")
    op.drop_index("ix_executions_attempt_id", table_name="executions")
    op.drop_index("ix_executions_run_id", table_name="executions")
    op.drop_table("executions")
