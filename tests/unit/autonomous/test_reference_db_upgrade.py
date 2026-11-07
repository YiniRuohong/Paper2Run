"""Reference-column upgrades use isolated databases, never the configured app DB."""
import importlib.util
from itertools import combinations
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from p2r_persistence.autonomous import AutonomousTaskRecord as Task
from p2r_persistence.db import Base, create_engine, create_session_factory, init_db
from p2r_persistence.models import ProjectRecord, UserRecord

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_COLUMNS = (
    sa.Column("reference_values_json", sa.JSON(), nullable=True),
    sa.Column("reference_scope", sa.String(8192), nullable=True),
    sa.Column("reference_sources_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
)


def migration_config(url):
    config = Config(str(ROOT / "packages/persistence/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages/persistence/alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def legacy_db(tmp_path, request):
    url = f"sqlite:///{tmp_path / 'legacy.sqlite3'}"
    engine = create_engine(url)
    config = migration_config(url)
    if getattr(request, "param", False):
        command.upgrade(config, "0020")
    else:
        # Simulate an unversioned create_all installation, with the actual
        # 0020 autonomous tables rather than a hand-written approximation.
        Base.metadata.create_all(engine, tables=[
            table for table in Base.metadata.sorted_tables
            if not table.name.startswith("autonomous_")
        ])
        spec = importlib.util.spec_from_file_location(
            "legacy_autonomous", ROOT / "packages/persistence/alembic/versions/0020_autonomous.py"
        )
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
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


def assert_reference_schema(engine):
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("autonomous_tasks")}
    assert isinstance(columns["reference_values_json"]["type"], sa.JSON)
    assert columns["reference_values_json"]["nullable"]
    assert columns["reference_scope"]["type"].length == 8192
    assert columns["reference_scope"]["nullable"]
    assert isinstance(columns["reference_sources_confirmed"]["type"], sa.Boolean)
    assert not columns["reference_sources_confirmed"]["nullable"]


def assert_old_task(engine):
    with create_session_factory(engine)() as session:
        task = session.get(Task, "old-task")
        assert task.objective == "Preserve the old task"
        assert task.reference_values_json is None
        assert task.reference_scope is None
        assert task.reference_sources_confirmed is False


@pytest.mark.parametrize("legacy_db", [False, True], indirect=True)
def test_old_create_all_cannot_upgrade_but_init_db_can(legacy_db):
    engine, _ = legacy_db
    before = set(sa.inspect(engine).get_table_names())
    # This was init_db's previous behavior: even existing rows cannot be read.
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        with pytest.raises(OperationalError, match="no such column.*reference_values_json"):
            session.scalars(sa.select(Task)).all()
    init_db(engine)
    init_db(engine)
    assert_reference_schema(engine)
    assert_old_task(engine)
    assert set(sa.inspect(engine).get_table_names()) == before
    with engine.connect() as connection:
        if "alembic_version" in before:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0020"
    # Server default also protects old clients that omit the new fields.
    with engine.begin() as connection:
        table = sa.Table("autonomous_tasks", sa.MetaData(), autoload_with=connection)
        values = dict(connection.execute(sa.select(table)).mappings().one())
        values["id"] = "legacy-writer"
        for column in REFERENCE_COLUMNS:
            values.pop(column.name)
        connection.execute(table.insert().values(values))
    with create_session_factory(engine)() as session:
        assert session.get(Task, "legacy-writer").reference_sources_confirmed is False


@pytest.mark.parametrize("legacy_db", [True], indirect=True)
@pytest.mark.parametrize("startup_first", [False, True])
def test_explicit_migration_and_startup_in_either_order(legacy_db, startup_first):
    engine, config = legacy_db
    if startup_first:
        init_db(engine)
    command.upgrade(config, "0021")
    init_db(engine)
    assert_reference_schema(engine)
    assert_old_task(engine)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0021"
    command.downgrade(config, "0020")
    assert not {column.name for column in REFERENCE_COLUMNS} & {
        column["name"] for column in sa.inspect(engine).get_columns("autonomous_tasks")
    }
    command.upgrade(config, "0021")
    assert_old_task(engine)


@pytest.mark.parametrize("present", [combo for count in range(1, 4) for combo in combinations(range(3), count)])
@pytest.mark.parametrize("legacy_db", [True], indirect=True)
@pytest.mark.parametrize("startup_first", [False, True])
def test_partial_upgrade_preserves_existing_reference_data(legacy_db, present, startup_first):
    engine, config = legacy_db
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for index in present:
            operations.add_column("autonomous_tasks", REFERENCE_COLUMNS[index])
        table = sa.Table("autonomous_tasks", sa.MetaData(), autoload_with=connection)
        expected = ({"score": 42}, "existing scope", True)
        connection.execute(table.update().values(**{
            REFERENCE_COLUMNS[index].name: expected[index] for index in present
        }))
    # Both entry points must tolerate a partially completed schema change.
    if startup_first:
        init_db(engine)
    command.upgrade(config, "0021")
    init_db(engine)
    init_db(engine)
    assert_reference_schema(engine)
    with create_session_factory(engine)() as session:
        task = session.get(Task, "old-task")
        for index in present:
            assert getattr(task, REFERENCE_COLUMNS[index].name) == expected[index]


@pytest.mark.parametrize("factory_name", ["create_app", "create_local_app"])
def test_api_and_desktop_factory_lifespan_on_legacy_database(legacy_db, tmp_path, monkeypatch, factory_name):
    from p2r_api import main
    from p2r_artifacts.store import LocalArtifactStore

    engine, _ = legacy_db
    monkeypatch.setenv("P2R_DEMO_DIR", str(tmp_path / "demo"))
    monkeypatch.setenv("P2R_AUTONOMOUS_DIR", str(tmp_path / "autonomous"))
    monkeypatch.setenv("P2R_DOCPARSE_DIR", str(tmp_path / "docparse"))
    app = getattr(main, factory_name)(
        db_url=str(engine.url), artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert_old_task(engine)
    finally:
        with app.state.runtime["session_factory"]() as session:
            session.get_bind().dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://unused/unused"])
def test_offline_0021_sql_generation(url):
    from io import StringIO

    config = migration_config(url)
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "0020:0021", sql=True)
    statements = output.getvalue()
    for column in REFERENCE_COLUMNS:
        assert statements.count(f"ADD COLUMN {column.name} ") == 1
    assert "DEFAULT false NOT NULL" in statements or "DEFAULT 0 NOT NULL" in statements


def test_fresh_database_repeated_init(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite3'}")
    try:
        init_db(engine)
        init_db(engine)
        assert_reference_schema(engine)
        assert "alembic_version" not in sa.inspect(engine).get_table_names()
        with create_session_factory(engine)() as session:
            assert session.scalars(sa.select(Task)).all() == []
    finally:
        engine.dispose()
