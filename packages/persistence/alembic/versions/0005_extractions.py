"""page-level extraction evidence"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)

def upgrade():
    op.create_table("extractions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("paper_id", sa.String(36), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False), sa.Column("schema_version", sa.String(16), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("content_sha256", sa.String(64), nullable=False), sa.Column("payload_json", sa.JSON(), nullable=False), sa.Column("error_json", sa.JSON()), sa.Column("created_at", TS, nullable=False), sa.Column("updated_at", TS, nullable=False), sa.UniqueConstraint("paper_id", "content_sha256", name="uq_extraction_paper_content"))
    op.create_index("ix_extractions_paper_id", "extractions", ["paper_id"])
    op.create_table("source_refs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("paper_id", sa.String(36), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False), sa.Column("extraction_id", sa.String(36), nullable=False), sa.Column("pdf_sha256", sa.String(64), nullable=False), sa.Column("page", sa.Integer(), nullable=False), sa.Column("section", sa.String(255)), sa.Column("table", sa.String(255)), sa.Column("row", sa.String(255)), sa.Column("quote", sa.Text(), nullable=False), sa.Column("bbox", sa.JSON()))
    op.create_index("ix_source_refs_paper_id", "source_refs", ["paper_id"])
    op.create_index("ix_source_refs_extraction_id", "source_refs", ["extraction_id"])

def downgrade():
    op.drop_table("source_refs")
    op.drop_table("extractions")
