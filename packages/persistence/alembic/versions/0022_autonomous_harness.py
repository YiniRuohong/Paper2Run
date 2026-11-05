"""Persist autonomous harness state, outcome and repair progress."""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    # init_db also repairs unversioned create_all installations. Inspect each
    # named column so startup-first and partially completed upgrades are safe.
    # Offline SQL describes a pristine 0021 schema; it cannot inspect a database.
    existing = set() if op.get_context().as_sql else {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("autonomous_tasks")
    }
    for column in (
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("harness_json", sa.JSON(), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("autonomous_tasks", column)


def downgrade():
    op.drop_column("autonomous_tasks", "harness_json")
    op.drop_column("autonomous_tasks", "outcome")
