from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from p2r_orchestration.service import OrchestrationService
from p2r_persistence.db import create_engine, create_session_factory, init_db


def build_runtime(
    *, db_url: str | None = None, session_factory: Any = None,
    supervisor: Any = None, artifact_store: Any = None,
) -> dict[str, Any]:
    if session_factory is None:
        engine = create_engine(db_url or os.getenv("P2R_DATABASE_URL", "sqlite:///p2r.sqlite3"))
        init_db(engine)
        session_factory = create_session_factory(engine)
    return {"session_factory": session_factory, "supervisor": supervisor, "artifact_store": artifact_store}


def get_service(request: Request) -> Generator[OrchestrationService, None, None]:
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        yield OrchestrationService(db, supervisor=runtime.get("supervisor"), artifact_store=runtime.get("artifact_store"))


def actor_id(request: Request) -> str:
    # Development-only actor context. Production authentication and CSRF are intentionally out of scope.
    return request.headers.get("X-Actor-ID", "dev-user")
