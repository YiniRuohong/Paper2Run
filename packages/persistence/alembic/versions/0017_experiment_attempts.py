"""experiment-level model attempts (paper->code codegen)

attempts.run_id becomes nullable and a nullable experiment_id FK is added so
AttemptRecord(source="model") rows can be created at experiment level BEFORE
any Run exists (experiment_id set + run_id NULL). Run-scoped semantics are
unchanged: template/repair attempts keep run_id set and experiment_id NULL,
and both existing unique constraints stay in place. A partial unique index
keeps (experiment_id, attempt_index) unique among run-unbound rows, matching
the create_all DDL installed by p2r_persistence.models.

Downgrade assumes no experiment-level attempts remain; delete them first.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_PARTIAL_WHERE = "experiment_id IS NOT NULL AND run_id IS NULL"


def _partial_unique_index() -> None:
    op.create_index(
        "uq_attempts_experiment_index", "attempts",
        ["experiment_id", "attempt_index"], unique=True,
        sqlite_where=sa.text(_PARTIAL_WHERE),
        postgresql_where=sa.text(_PARTIAL_WHERE),
    )


def upgrade():
    with op.batch_alter_table("attempts") as batch:
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("experiment_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_attempts_experiment_id", "experiments",
            ["experiment_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_attempts_experiment_id", "attempts", ["experiment_id"])
    _partial_unique_index()


def downgrade():
    op.drop_index("uq_attempts_experiment_index", table_name="attempts")
    op.drop_index("ix_attempts_experiment_id", table_name="attempts")
    with op.batch_alter_table("attempts") as batch:
        batch.drop_constraint("fk_attempts_experiment_id", type_="foreignkey")
        batch.drop_column("experiment_id")
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=False)
