"""parsed paper Markdown status and metadata (paper_texts)

One row per parse attempt for a paper's source PDF. The Markdown body itself
lives in the content-addressed artifact store; this table records the parse
status, backend identity, page coverage, and chunk map. Rows cascade with
their paper.

Downgrade drops the table and its paper_id index.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "paper_texts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("text_sha256", sa.String(64)),
        sa.Column("backend", sa.String(32)),
        sa.Column("backend_version", sa.String(64)),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("pages_parsed", sa.Integer(), nullable=False),
        sa.Column("chunk_map_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_json", sa.JSON()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("status IN ('parsing', 'ready', 'failed')", name="ck_paper_text_status"),
    )
    op.create_index("ix_paper_texts_paper_id", "paper_texts", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_texts_paper_id", table_name="paper_texts")
    op.drop_table("paper_texts")
