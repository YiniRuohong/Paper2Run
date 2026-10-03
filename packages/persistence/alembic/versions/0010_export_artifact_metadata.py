from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("exports", sa.Column("artifact_id", sa.String(64)))
    op.add_column("exports", sa.Column("storage_key", sa.String(1024)))
    op.add_column("exports", sa.Column("sha256", sa.String(64)))
    op.add_column("exports", sa.Column("bytes", sa.BigInteger()))
    op.add_column("exports", sa.Column("media_type", sa.String(255)))
    op.create_index("ix_exports_artifact_id", "exports", ["artifact_id"])


def downgrade():
    op.drop_index("ix_exports_artifact_id", table_name="exports")
    for name in ("media_type", "bytes", "sha256", "storage_key", "artifact_id"):
        op.drop_column("exports", name)
