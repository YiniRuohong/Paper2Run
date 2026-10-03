from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_index", sa.BigInteger(), nullable=False),
        sa.Column("repair_ordinal", sa.BigInteger()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("base_code_hash", sa.String(64), nullable=False),
        sa.Column("patch_hash", sa.String(64)),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("code_manifest_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "attempt_index", name="uq_attempts_run_index"),
        sa.UniqueConstraint("id", "run_id", name="uq_attempts_id_run"),
        sa.CheckConstraint("source IN ('template','model','repair')", name="ck_attempt_source"),
        sa.CheckConstraint("status IN ('candidate','proposed','validated','rejected')", name="ck_attempt_status"),
    )
    op.create_index("ix_attempts_run_id", "attempts", ["run_id"])
    op.create_table(
        "repair_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repair_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("base_code_hash", sa.String(64), nullable=False),
        sa.Column("patch_hash", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "repair_ordinal", name="uq_repair_proposals_attempt_ordinal"),
        sa.ForeignKeyConstraint(["attempt_id", "run_id"], ["attempts.id", "attempts.run_id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('proposed','accepted','rejected','needs_revision')", name="ck_repair_proposal_status"),
    )
    op.create_index("ix_repair_proposals_run_id", "repair_proposals", ["run_id"])
    op.create_index("ix_repair_proposals_attempt_id", "repair_proposals", ["attempt_id"])


def downgrade():
    op.drop_index("ix_repair_proposals_attempt_id", table_name="repair_proposals")
    op.drop_index("ix_repair_proposals_run_id", table_name="repair_proposals")
    op.drop_table("repair_proposals")
    op.drop_index("ix_attempts_run_id", table_name="attempts")
    op.drop_table("attempts")
