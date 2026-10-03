"""paper2run experiment protocol"""
from alembic import op
import sqlalchemy as sa
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)

def upgrade():
    op.create_table("experiments", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("paper_id", sa.String(36), sa.ForeignKey("papers.id"), nullable=False), sa.Column("extraction_id", sa.String(36), sa.ForeignKey("extractions.id"), nullable=False), sa.Column("candidate_experiment_id", sa.String(255), nullable=False), sa.Column("recipe_id", sa.String(255), nullable=False), sa.Column("recipe_version", sa.String(64), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("head_revision_id", sa.String(36)), sa.Column("repairs_used", sa.BigInteger(), nullable=False), sa.Column("repairs_limit", sa.BigInteger(), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.CheckConstraint("status IN ('draft','ready','active','needs_revision','closed')", name="ck_experiment_status"))
    op.create_index("ix_experiments_project_id", "experiments", ["project_id"])
    op.create_index("ix_experiments_paper_id", "experiments", ["paper_id"])
    op.create_table("spec_revisions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("experiment_id", sa.String(36), sa.ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False), sa.Column("revision_no", sa.BigInteger(), nullable=False), sa.Column("spec_hash", sa.String(64), nullable=False), sa.Column("policy_hash", sa.String(64), nullable=False), sa.Column("spec_json", sa.JSON(), nullable=False), sa.Column("validation_json", sa.JSON(), nullable=False), sa.Column("comparison_json", sa.JSON(), nullable=False), sa.Column("budget_json", sa.JSON(), nullable=False), sa.Column("created_at", TS, nullable=False), sa.UniqueConstraint("experiment_id", "revision_no", name="uq_spec_revision_number"))
    op.create_index("ix_spec_revisions_experiment_id", "spec_revisions", ["experiment_id"])
    op.create_table("confirmations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision_id", sa.String(36), sa.ForeignKey("spec_revisions.id"), nullable=False, unique=True), sa.Column("actor_id", sa.String(36), sa.ForeignKey("identity_users.id"), nullable=False), sa.Column("spec_hash", sa.String(64), nullable=False), sa.Column("policy_hash", sa.String(64), nullable=False), sa.Column("accepted_issue_codes", sa.JSON(), nullable=False), sa.Column("confirmed_at", TS, nullable=False))

def downgrade():
    op.drop_table("confirmations"); op.drop_table("spec_revisions"); op.drop_index("ix_experiments_paper_id", "experiments"); op.drop_index("ix_experiments_project_id", "experiments"); op.drop_table("experiments")
