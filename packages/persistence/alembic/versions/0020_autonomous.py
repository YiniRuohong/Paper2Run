"""Independent autonomous configuration, tasks and ordered event journal."""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table("autonomous_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False), *_timestamps(),
        sa.UniqueConstraint("project_id", "version", name="uq_autonomous_config_version"))
    op.create_index("ix_autonomous_configs_project_id", "autonomous_configs", ["project_id"])
    op.create_table("autonomous_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("objective", sa.String(8192), nullable=False),
        *[sa.Column(name, sa.JSON(), nullable=False) for name in
          ("config_snapshot", "context_json", "skills_json", "usage_json", "jobs_json", "artifacts_json")],
        *[sa.Column(name, sa.JSON()) for name in
          ("protocol_json", "report_json", "error_json", "pending_decision_json")],
        sa.Column("session_file", sa.String(2048)),
        sa.Column("event_sequence", sa.Integer(), nullable=False), *_timestamps())
    op.create_index("ix_autonomous_tasks_project_id", "autonomous_tasks", ["project_id"])
    op.create_table("autonomous_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("autonomous_tasks.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_autonomous_event_sequence"))
    op.create_index("ix_autonomous_events_task_id", "autonomous_events", ["task_id"])
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"CREATE TRIGGER IF NOT EXISTS p2r_autonomous_events_{operation.lower()} "
                f"BEFORE {operation} ON autonomous_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))


def downgrade():
    for table in ("autonomous_events", "autonomous_tasks", "autonomous_configs"):
        op.drop_table(table)
