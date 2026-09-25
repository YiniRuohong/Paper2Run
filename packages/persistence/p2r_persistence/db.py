from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine as _create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


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


def init_db(engine) -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    install_append_only_guards(engine)


def sqlite_test_engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
