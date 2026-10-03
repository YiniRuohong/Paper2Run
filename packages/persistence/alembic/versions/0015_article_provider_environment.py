"""environment checks and execution decision snapshot"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "environment_checks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("experiment_id", sa.String(36), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("checker_version", sa.String(64), nullable=False),
        sa.Column("requirements_json", sa.JSON(), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("suggestions_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["spec_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_environment_checks_project_id", "environment_checks", ["project_id"])
    op.create_index("ix_environment_checks_experiment_id", "environment_checks", ["experiment_id"])
    op.create_index("ix_environment_checks_revision_id", "environment_checks", ["revision_id"])

    op.create_table(
        "ssh_verifications",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("environment_check_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("experiment_id", sa.String(36), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("target_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_check_id"], ["environment_checks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["spec_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ssh_verifications_environment_check_id", "ssh_verifications", ["environment_check_id"])
    op.create_index("ix_ssh_verifications_project_id", "ssh_verifications", ["project_id"])
    op.create_index("ix_ssh_verifications_experiment_id", "ssh_verifications", ["experiment_id"])
    op.create_index("ix_ssh_verifications_revision_id", "ssh_verifications", ["revision_id"])


def downgrade():
    op.drop_index("ix_ssh_verifications_revision_id", table_name="ssh_verifications")
    op.drop_index("ix_ssh_verifications_experiment_id", table_name="ssh_verifications")
    op.drop_index("ix_ssh_verifications_project_id", table_name="ssh_verifications")
    op.drop_index("ix_ssh_verifications_environment_check_id", table_name="ssh_verifications")
    op.drop_table("ssh_verifications")
    op.drop_index("ix_environment_checks_revision_id", table_name="environment_checks")
    op.drop_index("ix_environment_checks_experiment_id", table_name="environment_checks")
    op.drop_index("ix_environment_checks_project_id", table_name="environment_checks")
    op.drop_table("environment_checks")
