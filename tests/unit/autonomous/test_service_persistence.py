import importlib.util
import hashlib
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import inspect, text

from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.autonomous import AutonomousService
from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import UserRecord, ProjectRecord, PaperRecord, PaperVersionRecord, PaperTextRecord


def test_migration_upgrade_downgrade_and_init_db_discovery(tmp_path):
    root = Path(__file__).resolve().parents[3]
    cfg = AlembicConfig(str(root / "packages/persistence/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "packages/persistence/alembic"))
    url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert {"autonomous_configs", "autonomous_tasks", "autonomous_events"} <= tables
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0022"
        triggers = conn.execute(text("select name from sqlite_master where type='trigger' and name like 'p2r_autonomous_events_%'")).scalars().all()
        assert len(triggers) == 2
    command.downgrade(cfg, "0019")
    assert "autonomous_tasks" not in inspect(engine).get_table_names()
    init_db(engine)
    assert "autonomous_tasks" in inspect(engine).get_table_names()
    engine.dispose()


def test_text_source_utf8_length_and_tampering(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    init_db(engine)
    factory = create_session_factory(engine)
    store = LocalArtifactStore(tmp_path / "sources")
    svc = AutonomousService(factory, tmp_path / "state", artifact_store=store)
    content = "文" * 120001
    address = store.put_bytes(content.encode())
    with factory() as db:
        db.add(UserRecord(id="owner", display_name="Owner")); db.flush()
        db.add(ProjectRecord(id="p", name="P", created_by="owner")); db.flush()
        paper = PaperRecord(project_id="p", sha256=address.sha256, artifact_key=str(address.path), bytes=address.bytes, page_count=1, processing_consent=True)
        db.add(paper); db.flush()
        version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=address.sha256, artifact_key=str(address.path), bytes=address.bytes, page_count=1, source_kind="text", media_type="text/plain")
        db.add(version); db.commit()
        context = svc.paper_context(db, "p", version.id)
        assert context["source_characters"] == 120001 and len(context["text"]) == 120000
        assert context["text_truncated"] is True and context["source_bytes"] == 360003
        address.path.write_text("tampered")
        context = svc.paper_context(db, "p", version.id)
        assert "text" not in context
    engine.dispose()


def test_pdf_context_uses_matching_persisted_parse(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    init_db(engine)
    factory = create_session_factory(engine)
    store = LocalArtifactStore(tmp_path / "sources")
    svc = AutonomousService(factory, tmp_path / "state", artifact_store=store)
    markdown = store.put_bytes(b"# Real parsed content")
    with factory() as db:
        db.add(UserRecord(id="owner", display_name="Owner")); db.flush()
        db.add(ProjectRecord(id="p", name="P", created_by="owner")); db.flush()
        paper = PaperRecord(project_id="p", sha256="a" * 64, artifact_key="unused", bytes=100, page_count=1, processing_consent=True)
        db.add(paper); db.flush()
        version = PaperVersionRecord(paper_id=paper.id, version=1, sha256=paper.sha256, artifact_key="unused", bytes=100, page_count=1)
        db.add(version)
        db.add(PaperTextRecord(paper_id=paper.id, source_sha256=paper.sha256, text_sha256=markdown.sha256, page_count=1, pages_parsed=1, status="ready"))
        db.commit()
        assert svc.paper_context(db, "p", version.id)["text"] == "# Real parsed content"
        version.sha256 = "b" * 64; db.commit()
        assert "text" not in svc.paper_context(db, "p", version.id)
    engine.dispose()
