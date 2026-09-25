"""initial persistence schema"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
_GUARD_PREFIX = "p2r_0001_append_only"


def upgrade() -> None:
    op.create_table("sessions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(255), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table("actions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("sequence_no", sa.BigInteger(), nullable=False), sa.Column("action_type", sa.String(100), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("session_id", "sequence_no", name="uq_actions_session_sequence"))
    op.create_index("ix_actions_session_id", "actions", ["session_id"])
    op.create_table("cell_executions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("cell_id", sa.String(255), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("input_text", sa.Text()), sa.Column("output_json", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_cell_executions_session_id", "cell_executions", ["session_id"])
    op.create_table("artifacts", sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("storage_key", sa.String(1024), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("bytes", sa.BigInteger(), nullable=False), sa.Column("media_type", sa.String(255)), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"])
    for table, name in (("environments", "Environment"), ("checkpoints", "Checkpoint")):
        op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(255), nullable=False), sa.Column("spec" if table == "environments" else "state", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index(f"ix_{table}_session_id", table, ["session_id"])
    op.create_table("events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("sequence_no", sa.BigInteger(), nullable=False), sa.Column("event_type", sa.String(100), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("session_id", "sequence_no", name="uq_events_session_sequence"))
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_table("idempotency_keys", sa.Column("id", sa.String(36), primary_key=True), sa.Column("scope", sa.String(255), nullable=False), sa.Column("key", sa.String(255), nullable=False), sa.Column("body_hash", sa.String(64), nullable=False), sa.Column("response_json", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"))
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("actions", "events"):
            op.execute(sa.text(f"CREATE TRIGGER IF NOT EXISTS {_GUARD_PREFIX}_{table}_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
            op.execute(sa.text(f"CREATE TRIGGER IF NOT EXISTS {_GUARD_PREFIX}_{table}_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
    elif bind.dialect.name == "postgresql":
        op.execute(sa.text(f"CREATE OR REPLACE FUNCTION {_GUARD_PREFIX}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only table'; END; $$"))
        for table in ("actions", "events"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_GUARD_PREFIX}_{table} ON {table}"))
            op.execute(sa.text(f"CREATE TRIGGER {_GUARD_PREFIX}_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {_GUARD_PREFIX}_guard()"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("actions", "events"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_GUARD_PREFIX}_{table}_update"))
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_GUARD_PREFIX}_{table}_delete"))
    elif bind.dialect.name == "postgresql":
        for table in ("actions", "events"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_GUARD_PREFIX}_{table} ON {table}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_GUARD_PREFIX}_guard()"))
    for table in ("idempotency_keys", "events", "checkpoints", "environments", "artifacts", "cell_executions", "actions", "sessions"):
        op.drop_table(table)
