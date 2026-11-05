from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import PaperRecord, PaperTextRecord, ProjectRecord, UserRecord

ROOT = Path(__file__).parents[3]
H = "a" * 64


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paper_texts.sqlite3'}")
    init_db(engine)
    return create_session_factory(engine)


def _seed_paper(session):
    user = UserRecord(display_name="paper text test", can_create_project=True)
    session.add(user)
    session.flush()
    project = ProjectRecord(name="paper text project", created_by=user.id)
    session.add(project)
    session.flush()
    paper = PaperRecord(project_id=project.id, sha256=H, artifact_key="paper", bytes=1, page_count=3)
    session.add(paper)
    session.flush()
    return paper


def _text(paper_id, **overrides):
    values = dict(
        paper_id=paper_id,
        source_sha256=H,
        text_sha256=None,
        backend="mineru",
        backend_version="1.0.0",
        page_count=3,
        pages_parsed=0,
        chunk_map_json=[],
        warnings_json=[],
        status="parsing",
        error_json=None,
    )
    values.update(overrides)
    return PaperTextRecord(**values)


def test_paper_text_roundtrip(tmp_path):
    factory = _session_factory(tmp_path)
    with factory.begin() as session:
        paper = _seed_paper(session)
        text = _text(paper.id, chunk_map_json=[{"page": 1, "offset": 0}], warnings_json=["slow"])
        session.add(text)
        session.flush()
        text_id = text.id
        assert text_id.startswith("ptx_")

    with factory() as session:
        loaded = session.scalar(select(PaperTextRecord).where(PaperTextRecord.paper_id == paper.id))
        assert loaded is not None
        assert loaded.id == text_id
        assert loaded.source_sha256 == H
        assert loaded.text_sha256 is None
        assert loaded.backend == "mineru"
        assert loaded.backend_version == "1.0.0"
        assert loaded.page_count == 3
        assert loaded.pages_parsed == 0
        assert loaded.chunk_map_json == [{"page": 1, "offset": 0}]
        assert loaded.warnings_json == ["slow"]
        assert loaded.status == "parsing"
        assert loaded.error_json is None
        assert loaded.created_at is not None
        assert loaded.updated_at is not None


def test_status_constraint(tmp_path):
    factory = _session_factory(tmp_path)
    with factory.begin() as session:
        paper = _seed_paper(session)
        session.add(_text(paper.id, status="ready"))

    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            paper = session.scalar(select(PaperRecord))
            session.add(_text(paper.id, status="done"))


def test_migration_heads():
    cfg = Config(str(ROOT / "packages/persistence/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "packages/persistence/alembic"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == "0022"


def test_migration_0019_creates_and_drops_paper_texts(tmp_path):
    cfg = Config(str(ROOT / "packages/persistence/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "packages/persistence/alembic"))
    url = f"sqlite:///{tmp_path / 'migrated.sqlite3'}"
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert "paper_texts" in inspect(engine).get_table_names()
    assert {ix["name"] for ix in inspect(engine).get_indexes("paper_texts")} == {"ix_paper_texts_paper_id"}

    command.downgrade(cfg, "0018")
    assert "paper_texts" not in inspect(engine).get_table_names()
