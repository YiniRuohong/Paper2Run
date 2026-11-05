"""Persist trusted reference values for autonomous verification."""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    # init_db also repairs unversioned create_all installations. Inspect each
    # named column so startup-first and partially completed upgrades are safe.
    # Offline SQL describes a pristine 0020 schema; it cannot inspect a database.
    existing = set() if op.get_context().as_sql else {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("autonomous_tasks")
    }
    for column in (
        sa.Column("reference_values_json", sa.JSON(), nullable=True),
        sa.Column("reference_scope", sa.String(8192), nullable=True),
        sa.Column("reference_sources_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    ):
        if column.name not in existing:
            op.add_column("autonomous_tasks", column)


def downgrade():
    op.drop_column("autonomous_tasks", "reference_sources_confirmed")
    op.drop_column("autonomous_tasks", "reference_scope")
    op.drop_column("autonomous_tasks", "reference_values_json")
