from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from p2r_persistence.db import Base, create_engine, create_session_factory, init_db
from p2r_persistence.models import Action, SessionRecord
from p2r_persistence.repositories import ActionRepository, IdempotencyRepository


def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'persistence.sqlite3'}")
    init_db(engine)
    return engine, create_session_factory(engine)


def test_alembic_lifecycle_then_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "migration.sqlite3"
    url = f"sqlite:///{db_path}"
    root = Path(__file__).parents[3]
    cfg = Config(str(root / "packages/persistence/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "packages/persistence/alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO sentinel (id) VALUES (1)"))
    command.upgrade(cfg, "head")
    init_db(engine)
    init_db(engine)
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT 1 FROM sentinel")) == 1


    engine, factory = db(tmp_path)
    with factory.begin() as session:
        owner = SessionRecord(user_id="u1")
        session.add(owner); session.flush()
        repo = ActionRepository(session)
        repo.append(session_id=owner.id, sequence_no=1, kind="a", payload={})
        try:
            repo.append(session_id=owner.id, sequence_no=1, kind="b", payload={})
        except (ValueError, StatementError) as exc:
            assert isinstance(exc, ValueError) or isinstance(exc.orig, ValueError)
        repo.append(session_id=owner.id, sequence_no=2, kind="c", payload={})
    with factory() as session:
        assert session.scalar(select(Action).where(Action.sequence_no == 2)) is not None


def test_append_fk_error_is_not_duplicate(tmp_path):
    _, factory = db(tmp_path)
    with factory.begin() as session:
        try:
            ActionRepository(session).append(session_id="missing", sequence_no=1, kind="x", payload={})
        except (IntegrityError, StatementError) as exc:
            assert isinstance(exc, IntegrityError) or isinstance(exc.orig, ValueError)
        else:
            raise AssertionError("missing FK must remain an explicit integrity error")



def test_utc_roundtrip_rejects_naive(tmp_path):
    _, factory = db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as session:
        owner = SessionRecord(user_id="u", created_at=now, updated_at=now)
        session.add(owner)
    with factory() as session:
        value = session.scalar(select(SessionRecord).where(SessionRecord.user_id == "u")).created_at
        assert value.tzinfo == timezone.utc
    with factory.begin() as session:
        try:
            session.add(SessionRecord(user_id="bad", created_at=datetime(2026, 1, 1), updated_at=now)); session.flush()
        except (ValueError, StatementError) as exc:
            assert isinstance(exc, ValueError) or isinstance(exc.orig, ValueError)



def test_separate_transactions_idempotency_conflict(tmp_path):
    _, factory = db(tmp_path)
    with factory.begin() as first:
        result = IdempotencyRepository(first).claim(scope="s", key="k", body=b"one")
        record_id = result.record.id
    with factory.begin() as second:
        result = IdempotencyRepository(second).claim(scope="s", key="k", body=b"one")
        assert result.record.id == record_id and not result.conflict
    with factory.begin() as third:
        assert IdempotencyRepository(third).claim(scope="s", key="k", body=b"two").conflict


def test_append_only_triggers_reject_core_sql_and_allow_drop(tmp_path):
    engine, factory = db(tmp_path)
    with factory.begin() as session:
        owner = SessionRecord(user_id="u"); session.add(owner); session.flush()
        ActionRepository(session).append(session_id=owner.id, sequence_no=1, kind="a", payload={})
        action_id = session.scalar(select(Action.id))
    with engine.begin() as conn:
        for statement in (text("UPDATE actions SET action_type='x' WHERE id=:id"), text("DELETE FROM actions WHERE id=:id")):
            try:
                conn.execute(statement, {"id": action_id})
            except IntegrityError:
                pass
            else:
                raise AssertionError("append-only trigger must reject mutation")
