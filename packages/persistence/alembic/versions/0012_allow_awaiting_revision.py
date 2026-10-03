from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_run_status", type_="check")
        batch.create_check_constraint(
            "ck_run_status",
            "status IN ('queued','created','running','evaluating','cancel_requested','completed','failed','cancelled','awaiting_revision')",
        )


def downgrade():
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_run_status", type_="check")
        batch.create_check_constraint(
            "ck_run_status",
            "status IN ('queued','created','running','evaluating','cancel_requested','completed','failed','cancelled')",
        )
