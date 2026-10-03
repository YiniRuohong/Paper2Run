from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table("run_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False), sa.Column("type", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False), sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"))
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table("exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("requested_formats_json", sa.JSON(), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False), sa.Column("error_json", sa.JSON()),
        sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False))
    op.create_index("ix_exports_run_id", "exports", ["run_id"])


def downgrade():
    op.drop_index("ix_exports_run_id", "exports"); op.drop_table("exports")
    op.drop_index("ix_run_events_run_id", "run_events"); op.drop_table("run_events")
