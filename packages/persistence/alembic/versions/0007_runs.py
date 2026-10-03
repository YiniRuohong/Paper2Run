from alembic import op
import sqlalchemy as sa
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)

def upgrade():
    op.create_table("runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("experiment_id", sa.String(36), sa.ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False), sa.Column("revision_id", sa.String(36), sa.ForeignKey("spec_revisions.id"), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("mode", sa.String(32), nullable=False), sa.Column("manifest_hash", sa.String(64), nullable=False), sa.Column("metrics_hash", sa.String(64)), sa.Column("report_hash", sa.String(64)), sa.Column("verdict", sa.String(64)), sa.Column("error_json", sa.JSON()), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.CheckConstraint("status IN ('created','running','evaluating','completed','failed')", name="ck_run_status"))
    op.create_index("ix_runs_project_id", "runs", ["project_id"]); op.create_index("ix_runs_experiment_id", "runs", ["experiment_id"]); op.create_index("ix_runs_revision_id", "runs", ["revision_id"])
    op.create_table("evaluations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True), sa.Column("metrics_json", sa.JSON(), nullable=False), sa.Column("verdict", sa.String(64), nullable=False), sa.Column("metrics_hash", sa.String(64), nullable=False), sa.Column("evaluator_version", sa.String(128), nullable=False))
    op.create_table("reports", sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True), sa.Column("content_json", sa.JSON(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False))

def downgrade():
    op.drop_table("reports"); op.drop_table("evaluations"); op.drop_index("ix_runs_revision_id", "runs"); op.drop_index("ix_runs_experiment_id", "runs"); op.drop_index("ix_runs_project_id", "runs"); op.drop_table("runs")
