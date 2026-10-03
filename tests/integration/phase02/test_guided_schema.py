from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, inspect, select
from sqlalchemy.exc import IntegrityError

from p2r_persistence.db import Base, create_engine, init_db
from p2r_persistence import models


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
NEW_TABLES = {"report_builds", "export_items", "export_artifacts", "demo_write_receipts"}


def database(tmp_path, revision="head"):
    root = Path(__file__).parents[3]
    cfg = Config(str(root / "packages/persistence/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "packages/persistence/alembic"))
    url = f"sqlite:///{tmp_path / 'guided.sqlite3'}"
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, revision)
    return cfg, create_engine(url)


def tables(engine):
    metadata = MetaData()
    metadata.reflect(engine)
    return metadata.tables


def seed_old(engine):
    t = tables(engine)
    with engine.begin() as conn:
        def insert(table_name, **values):
            for column in ("created_at", "updated_at"):
                if column in t[table_name].c:
                    values[column] = NOW
            conn.execute(t[table_name].insert().values(**values))

        insert("identity_users", id="actor", display_name="Actor", status="active", can_create_project=True)
        insert("projects", id="project", name="demo", description="", created_by="actor", version=1, deletion_state="active")
        insert("papers", id="paper", project_id="project", status="ready", sha256="a" * 64, artifact_key="source", bytes=1, page_count=1, processing_consent=True)
        insert("paper_versions", id="version", paper_id="paper", version=1, sha256="a" * 64, artifact_key="source", bytes=1, page_count=1)
        insert("extractions", id="extraction", paper_id="paper", schema_version="1.0", status="ready", content_sha256="b" * 64, payload_json={})
        insert("experiments", id="experiment", project_id="project", paper_id="paper", extraction_id="extraction", candidate_experiment_id="candidate", recipe_id="fixture", recipe_version="1", status="ready", repairs_used=0, repairs_limit=5)
        insert("spec_revisions", id="revision", experiment_id="experiment", revision_no=1, spec_hash="c" * 64, policy_hash="d" * 64, spec_json={}, validation_json={}, comparison_json={}, budget_json={})
        insert("runs", id="run", project_id="project", experiment_id="experiment", revision_id="revision", status="completed", mode="local-cpu", manifest_hash="e" * 64)
        insert("reports", id="report", run_id="run", content_json={"legacy": ["保留", 1]}, content_hash="f" * 64)
        for export_id, size in (("complete", 0), ("incomplete", None)):
            insert("exports", id=export_id, run_id="run", status="succeeded", requested_formats_json=["report_json"], items_json=[{"format": "report_json", "legacy": True}], artifact_id="a" * 64, storage_key="exports/report.json", sha256="a" * 64, bytes=size, media_type="application/json")


def test_empty_upgrade_and_orm_schema(tmp_path):
    _, engine = database(tmp_path)
    assert NEW_TABLES <= set(inspect(engine).get_table_names())
    for name in NEW_TABLES | {"papers", "paper_versions", "runs", "exports"}:
        assert set(Base.metadata.tables[name].c.keys()) == {c["name"] for c in inspect(engine).get_columns(name)}
    assert not any("active_report_build_id" in fk["constrained_columns"] for fk in inspect(engine).get_foreign_keys("runs"))


def test_upgrade_preserves_legacy_and_downgrade(tmp_path):
    cfg, engine = database(tmp_path, "0013")
    seed_old(engine)
    old = tables(engine)
    with engine.connect() as conn:
        before = {name: conn.execute(select(old[name])).mappings().all() for name in ("reports", "exports")}
    command.upgrade(cfg, "head")
    t = tables(engine)
    assert NEW_TABLES <= set(t)
    with engine.connect() as conn:
        for name in ("papers", "paper_versions"):
            row = conn.execute(select(t[name])).mappings().one()
            assert (row["source_kind"], row["media_type"]) == ("pdf", "application/pdf")
        assert conn.execute(select(t["reports"])).mappings().all() == before["reports"]
        for row in conn.execute(select(t["exports"])).mappings():
            original = next(r for r in before["exports"] if r["id"] == row["id"])
            assert all(row[key] == value for key, value in original.items())
            assert row["operation_id"] is None
        item = conn.execute(select(t["export_items"])).mappings().one()
        assert item["export_id"] == "complete"
        assert item["format"] == "report_json" and item["status"] == "ready"
        assert item["bytes"] == 0 and item["sha256"] == "a" * 64
        assert item["storage_key"] == "exports/report.json"
        assert item["media_type"] == "application/json"
        assert not conn.execute(select(t["report_builds"])).all()
        assert not conn.execute(select(t["export_artifacts"])).all()
        assert conn.execute(select(t["runs"].c.active_report_build_id)).scalar_one() is None
    command.downgrade(cfg, "0013")
    t = tables(engine)
    assert not NEW_TABLES & set(t)
    with engine.connect() as conn:
        for name in before:
            assert conn.execute(select(t[name])).mappings().all() == before[name]
    assert "source_kind" not in t["papers"].c
    assert "source_kind" not in t["paper_versions"].c
    assert "active_report_build_id" not in t["runs"].c
    assert "operation_id" not in t["exports"].c


@pytest.mark.parametrize("schema", ["migration", "orm"])
def test_database_constraints(tmp_path, schema):
    if schema == "migration":
        _, engine = database(tmp_path)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'orm.sqlite3'}")
        init_db(engine)
    assert NEW_TABLES <= set(inspect(engine).get_table_names())
    seed_old(engine)
    # Exercise SQLAlchemy defaults, not explicit fixture IDs: PostgreSQL enforces
    # VARCHAR lengths whereas SQLite silently accepts oversized identifiers.
    from sqlalchemy.orm import Session
    with Session(engine) as session, session.begin():
        session.add(models.OperationRecord(id="default-op", kind="report_build", resource_id="run"))
        session.flush()
        build = models.ReportBuildRecord(run_id="run", operation_id="default-op", generation=99, input_json={}, input_hash="a" * 64)
        item = models.ExportItemRecord(export_id="complete", format="report_markdown", status="pending")
        session.add_all([build, item])
        session.flush()
        assert build.id.startswith("rpb_") and len(build.id) == 36
        assert item.id.startswith("exi_") and len(item.id) == 36
        assert build.created_at.tzinfo == timezone.utc
    t = tables(engine)
    with engine.begin() as conn:
        for op_id in ("op1", "op2"):
            conn.execute(t["operations"].insert().values(id=op_id, kind="report_build", resource_id="run", status="queued", created_at=NOW, updated_at=NOW))
    build = dict(id="build1", run_id="run", operation_id="op1", generation=1, status="pending", input_json={}, input_hash="b" * 64, created_at=NOW, updated_at=NOW)
    item = dict(id="item1", export_id="complete", format="report_json", status="pending")
    receipt = dict(actor_id="actor", key="key", target_path="/api/v1/demo/bootstrap", request_sha256="c" * 64, response_json={"project_id": "project"}, project_id="project")
    for name, value, duplicate in (
        ("report_builds", build, {**build, "id": "build2", "operation_id": "op2"}),
        ("export_items", item, {**item, "id": "item2"}),
        ("demo_write_receipts", receipt, receipt),
    ):
        with engine.begin() as conn:
            conn.execute(t[name].insert().values(**value))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(t[name].insert().values(**duplicate))
    for override in ({"status": "unknown", "operation_id": "op2"}, {"status": "ready"}):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(t["report_builds"].insert().values(**{**build, "id": "build3", "generation": 2, **override}))
