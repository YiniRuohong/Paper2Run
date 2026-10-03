"""guided demo persistence contract"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("papers", sa.Column("source_kind", sa.String(32), nullable=False, server_default="pdf"))
    op.add_column("papers", sa.Column("media_type", sa.String(255), nullable=False, server_default="application/pdf"))
    op.add_column("paper_versions", sa.Column("source_kind", sa.String(32), nullable=False, server_default="pdf"))
    op.add_column("paper_versions", sa.Column("media_type", sa.String(255), nullable=False, server_default="application/pdf"))
    op.add_column("runs", sa.Column("active_report_build_id", sa.String(36), nullable=True))
    op.add_column("exports", sa.Column("report_hash", sa.String(64), nullable=True))
    op.add_column("exports", sa.Column("operation_id", sa.String(36), nullable=True))

    op.create_table(
        "report_builds",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_report_build_operation"),
        sa.UniqueConstraint("run_id", "generation", name="uq_report_build_generation"),
        sa.CheckConstraint("status IN ('pending','generating','ready','failed')", name="ck_report_build_status"),
    )
    op.create_index("ix_report_builds_run_id", "report_builds", ["run_id"])
    op.create_table(
        "export_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("export_id", sa.String(36), nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(64)), sa.Column("storage_key", sa.String(1024)),
        sa.Column("sha256", sa.String(64)), sa.Column("bytes", sa.BigInteger()),
        sa.Column("media_type", sa.String(255)), sa.Column("filename", sa.String(255)),
        sa.Column("error_json", sa.JSON()), sa.Column("reuse_key", sa.String(64)),
        sa.ForeignKeyConstraint(["export_id"], ["exports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("export_id", "format", name="uq_export_item_format"),
    )
    op.create_index("ix_export_items_export_id", "export_items", ["export_id"])
    op.create_table(
        "export_artifacts",
        sa.Column("reuse_key", sa.String(64), nullable=False), sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False), sa.Column("format", sa.String(32), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False), sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False), sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("reuse_key"),
    )
    op.create_index("ix_export_artifacts_run_id", "export_artifacts", ["run_id"])
    op.create_table(
        "demo_write_receipts",
        sa.Column("actor_id", sa.String(255), nullable=False), sa.Column("key", sa.String(255), nullable=False),
        sa.Column("target_path", sa.String(255), nullable=False), sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False), sa.Column("project_id", sa.String(36)),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("actor_id", "key"), sa.UniqueConstraint("actor_id", "key", name="uq_demo_write_receipt_actor_key"),
    )

    # The legacy worker only built JSON. Do not interpret requested formats as
    # evidence of files, or invent a reusable artifact without a rules version.
    # INSERT ... SELECT also works in offline SQL generation on both dialects.
    op.execute(sa.text("""
        INSERT INTO export_items
            (id, export_id, format, status, artifact_id, storage_key, sha256, bytes, media_type, filename)
        SELECT id, id, 'report_json', 'ready', artifact_id, storage_key, sha256, bytes, media_type, 'report.json'
        FROM exports
        WHERE status = 'succeeded' AND media_type = 'application/json'
          AND artifact_id IS NOT NULL AND artifact_id <> ''
          AND storage_key IS NOT NULL AND storage_key <> ''
          AND sha256 IS NOT NULL AND length(sha256) = 64
          AND bytes IS NOT NULL AND bytes >= 0
    """))


def downgrade():
    op.drop_table("demo_write_receipts")
    op.drop_index("ix_export_artifacts_run_id", table_name="export_artifacts")
    op.drop_table("export_artifacts")
    op.drop_index("ix_export_items_export_id", table_name="export_items")
    op.drop_table("export_items")
    op.drop_index("ix_report_builds_run_id", table_name="report_builds")
    op.drop_table("report_builds")
    op.drop_column("exports", "operation_id")
    op.drop_column("exports", "report_hash")
    op.drop_column("runs", "active_report_build_id")
    op.drop_column("paper_versions", "media_type")
    op.drop_column("paper_versions", "source_kind")
    op.drop_column("papers", "media_type")
    op.drop_column("papers", "source_kind")
