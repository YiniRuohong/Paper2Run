"""Harness-state columns (revision 0022) upgrade in either startup order."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from p2r_persistence.autonomous import AutonomousTaskRecord as Task
from p2r_persistence.db import Base, create_engine, create_session_factory, init_db
from p2r_persistence.models import ProjectRecord, UserRecord

ROOT = Path(__file__).resolve().parents[3]
HARNESS_COLUMNS = {"outcome", "harness_json"}


def migration_config(url):
    config = Config(str(ROOT / "packages/persistence/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages/persistence/alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def legacy_db(tmp_path, request):
    """Database at revision 0021 (migrated) or unversioned create_all at 0021 shape."""
    url = f"sqlite:///{tmp_path / 'legacy.sqlite3'}"
    engine = create_engine(url)
    config = migration_config(url)
    if getattr(request, "param", True):
        command.upgrade(config, "0021")
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(UserRecord(id="owner", display_name="Owner"))
        session.flush()
        session.add(ProjectRecord(id="project", name="Project", created_by="owner"))
    table = sa.Table("autonomous_tasks", sa.MetaData(), autoload_with=engine)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id="old-task", project_id="project", actor_id="owner", state="paused",
            objective="Preserve the old task", config_snapshot={}, context_json={},
            skills_json=[], usage_json={}, jobs_json={}, artifacts_json=[],
            event_sequence=0, created_at=now, updated_at=now,
        ))
    try:
        yield engine, config
    finally:
        engine.dispose()


def assert_harness_schema(engine):
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("autonomous_tasks")}
    assert HARNESS_COLUMNS <= set(columns)
    assert columns["outcome"]["type"].length == 32
    assert columns["outcome"]["nullable"]
    assert isinstance(columns["harness_json"]["type"], sa.JSON)
    assert columns["harness_json"]["nullable"]


def assert_old_task(engine):
    with create_session_factory(engine)() as session:
        task = session.get(Task, "old-task")
        assert task.objective == "Preserve the old task"
        assert task.harness_json is None
        assert task.outcome is None


@pytest.mark.parametrize("startup_first", [False, True])
def test_harness_columns_added_in_either_order(legacy_db, startup_first):
    engine, config = legacy_db
    if startup_first:
        init_db(engine)
    command.upgrade(config, "0022")
    init_db(engine)
    assert_harness_schema(engine)
    assert_old_task(engine)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0022"
    command.downgrade(config, "0021")
    assert not HARNESS_COLUMNS & {c["name"] for c in sa.inspect(engine).get_columns("autonomous_tasks")}
    command.upgrade(config, "0022")
    assert_old_task(engine)


def test_init_db_alone_adds_harness_columns(legacy_db):
    engine, _ = legacy_db
    init_db(engine)
    init_db(engine)
    assert_harness_schema(engine)
    assert_old_task(engine)


def test_fresh_database_has_harness_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite3'}")
    try:
        init_db(engine)
        assert_harness_schema(engine)
    finally:
        engine.dispose()
