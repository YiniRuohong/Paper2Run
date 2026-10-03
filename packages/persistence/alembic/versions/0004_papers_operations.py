"""paper ingestion models"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)

def upgrade():
    op.create_table("papers", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("title", sa.String(500)), sa.Column("status", sa.String(32), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("artifact_key", sa.String(1024), nullable=False), sa.Column("bytes", sa.BigInteger(), nullable=False), sa.Column("page_count", sa.Integer(), nullable=False), sa.Column("processing_consent", sa.Boolean(), nullable=False), sa.Column("error_json", sa.JSON()), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.CheckConstraint("status IN ('uploaded', 'parsing', 'ready', 'failed', 'unsupported')", name="ck_paper_status"))
    op.create_index("ix_papers_project_id", "papers", ["project_id"])
    op.create_table("paper_versions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("paper_id", sa.String(36), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("artifact_key", sa.String(1024), nullable=False), sa.Column("bytes", sa.BigInteger(), nullable=False), sa.Column("page_count", sa.Integer(), nullable=False), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.UniqueConstraint("paper_id", "version", name="uq_paper_version"))
    op.create_index("ix_paper_versions_paper_id", "paper_versions", ["paper_id"])
    op.create_table("operations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kind", sa.String(64), nullable=False), sa.Column("resource_id", sa.String(36), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("error_json", sa.JSON()), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False))
    op.create_index("ix_operations_resource_id", "operations", ["resource_id"])

def downgrade():
    op.drop_table("operations"); op.drop_table("paper_versions"); op.drop_table("papers")
