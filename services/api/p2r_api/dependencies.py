from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Request

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import AuthSessionRecord
from .auth import IdentityService, SessionRequiredError

# The single local user. The id predates this module (existing local databases
# own their projects under it, and the web UI sends it as X-Actor-ID), so it
# must not change.
LOCAL_ACTOR_ID = "demo-user"


def data_dir() -> Path:
    """Root of the local runtime's data; ``P2R_DEMO_DIR`` keeps its historical name and default."""
    return Path(os.environ.get("P2R_DEMO_DIR", ".p2r-demo")).resolve()


def docparse_root() -> Path:
    configured = os.environ.get("P2R_DOCPARSE_DIR")
    if configured:
        return Path(configured).resolve()
    return data_dir() / "docparse"


def build_runtime(*, db_url: str | None = None, session_factory: Any = None,
                  artifact_store: Any = None,
                  allowed_origin: str | None = None, secure_cookies: bool = True,
                  development_auth: bool = False, docparse_service: Any = None,
                  local_actor: str | None = None) -> dict[str, Any]:
    injected_persistence = session_factory is not None or db_url is not None
    if session_factory is None:
        engine = create_engine(db_url or os.getenv("P2R_DATABASE_URL", "sqlite:///p2r.sqlite3"))
        init_db(engine)
        session_factory = create_session_factory(engine)
    if docparse_service is None:
        from p2r_orchestration.docparse_service import DocParseService
        docparse_service = DocParseService(docparse_root())
    from p2r_orchestration.autonomous import AutonomousService
    autonomous_root = None
    if not os.getenv("P2R_AUTONOMOUS_DIR") and not os.getenv("P2R_DEMO_DIR"):
        if injected_persistence:
            with session_factory() as db:
                bind = db.get_bind()
                url = bind.url
            if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
                database = Path(url.database).resolve()
                autonomous_root = database.parent / (database.name + ".autonomous")
            elif artifact_store is not None and hasattr(artifact_store, "root"):
                autonomous_root = Path(artifact_store.root).resolve() / "autonomous-control"
        elif artifact_store is not None and hasattr(artifact_store, "root"):
            autonomous_root = Path(artifact_store.root).resolve() / "autonomous-control"
    autonomous = AutonomousService(session_factory, autonomous_root, artifact_store=artifact_store)
    return {"autonomous_service": autonomous, "session_factory": session_factory, "artifact_store": artifact_store,
            "allowed_origin": allowed_origin, "secure_cookies": secure_cookies,
            "development_auth": development_auth,
            "docparse_service": docparse_service,
            "local_actor": local_actor}


def get_identity(request: Request) -> IdentityService:
    runtime = request.app.state.runtime
    db = runtime["session_factory"]()
    # Request middleware closes this session after response.
    request.state.identity_db = db
    return IdentityService(db)


def current_auth(request: Request) -> tuple[IdentityService, AuthSessionRecord, str]:
    runtime = request.app.state.runtime
    if runtime.get("development_auth"):
        raise SessionRequiredError("session required")
    secret = request.cookies.get("p2r_session")
    service = get_identity(request)
    row = service.get_current_session(secret)
    return service, row, secret


def actor_id(request: Request) -> str:
    local = request.app.state.runtime.get("local_actor")
    if local:
        from .auth import ResourceNotFoundError
        if request.headers.get("X-Actor-ID", local) != local:
            raise ResourceNotFoundError("resource not found")
        return local
    if request.app.state.runtime.get("development_auth"):
        return request.headers.get("X-Actor-ID", "dev-user")
    _, row, _ = current_auth(request)
    return row.user_id
