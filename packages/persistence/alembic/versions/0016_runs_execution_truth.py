"""run execution truth: pinned environment check and confirmed target"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("environment_check_id", sa.String(36), nullable=True))
    op.add_column("runs", sa.Column("target_json", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("runs", "target_json")
    op.drop_column("runs", "environment_check_id")
