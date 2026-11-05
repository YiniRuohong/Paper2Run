from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Boolean, Column, JSON, String, create_engine as _create_engine, event, false, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn


class Base(DeclarativeBase):
    pass


_APPEND_ONLY_TABLES = ("actions", "events")
_GUARD_PREFIX = "p2r_0001_append_only"


def install_append_only_guards(engine: Engine) -> None:
    """Install immutable-log guards for both create_all and migration paths."""
    dialect = engine.dialect.name
    with engine.begin() as connection:
        if dialect == "sqlite":
            for table in _APPEND_ONLY_TABLES:
                connection.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {_GUARD_PREFIX}_{table}_update "
                    f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
                ))
                connection.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {_GUARD_PREFIX}_{table}_delete "
                    f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
                ))
        elif dialect == "postgresql":
            # The migration and create_all paths share these names. Replacing
            # the function and recreating only our triggers makes init_db safe
            # after upgrade head and on repeated calls.
            connection.execute(text(
                f"CREATE OR REPLACE FUNCTION {_GUARD_PREFIX}_guard() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only table'; END; $$"
            ))
            for table in _APPEND_ONLY_TABLES:
                connection.execute(text(
                    f"DROP TRIGGER IF EXISTS {_GUARD_PREFIX}_{table} ON {table}"
                ))
                connection.execute(text(
                    f"CREATE TRIGGER {_GUARD_PREFIX}_{table} "
                    f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                    f"EXECUTE FUNCTION {_GUARD_PREFIX}_guard()"
                ))


def create_engine(url: str = "sqlite:///p2r.sqlite3", *, echo: bool = False):
    engine = _create_engine(url, echo=echo, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def create_session_factory(engine):
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _ensure_reference_columns(engine: Engine) -> None:
    """Bridge pre-0021 create_all databases without adopting migration history.

    Keep this limited to additive reference metadata: create_all installations
    cannot safely replay the full Alembic chain. Revision 0021 also checks for
    these columns so either initialization order is safe.
    """
    columns = (
        Column("reference_values_json", JSON(), nullable=True),
        Column("reference_scope", String(8192), nullable=True),
        Column("reference_sources_confirmed", Boolean(), nullable=False, server_default=false()),
    )
    with engine.begin() as connection:
        existing = {column["name"] for column in inspect(connection).get_columns("autonomous_tasks")}
        for column in columns:
            if column.name not in existing:
                definition = CreateColumn(column).compile(dialect=connection.dialect)
                connection.execute(text(f"ALTER TABLE autonomous_tasks ADD COLUMN {definition}"))


def init_db(engine) -> None:
    from . import models, autonomous  # noqa: F401
    Base.metadata.create_all(engine)
    _ensure_reference_columns(engine)
    install_append_only_guards(engine)


def sqlite_test_engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
