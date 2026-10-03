from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("jobs", "fencing_token")
