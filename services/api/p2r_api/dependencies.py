from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from p2r_persistence.db import create_engine, create_session_factory, init_db
from p2r_persistence.models import AuthSessionRecord
from .auth import IdentityService, SessionRequiredError, csrf_token


def build_runtime(*, db_url: str | None = None, session_factory: Any = None,
                  supervisor: Any = None, artifact_store: Any = None,
                  allowed_origin: str | None = None, secure_cookies: bool = True,
                  development_auth: bool = False) -> dict[str, Any]:
    if session_factory is None:
        engine = create_engine(db_url or os.getenv("P2R_DATABASE_URL", "sqlite:///p2r.sqlite3"))
        init_db(engine)
        session_factory = create_session_factory(engine)
    from p2r_orchestration.provider_sessions import ProviderSecretStore
    from p2r_orchestration.environment_checker import EnvironmentChecker
    from p2r_orchestration.session_extraction import SessionExtractionService
    from p2r_orchestration.ssh_executor import FakeSSHVerifier

    store = ProviderSecretStore()
    return {"session_factory": session_factory, "supervisor": supervisor, "artifact_store": artifact_store,
            "allowed_origin": allowed_origin, "secure_cookies": secure_cookies,
            "development_auth": development_auth,
            # Runtime-only: never persisted, never serialized into DTOs.
            "provider_secret_store": store,
            # Real OpenAI-compatible extraction seam; resolves live sessions
            # from the same runtime-only store instance as above (secrets stay
            # process-local, transport injectable for tests).
            "extraction_service": SessionExtractionService(store),
            "environment_checker": EnvironmentChecker(),
            # Default fake SSH verifier: no network I/O in this phase; tests
            # (and later a controlled transport adapter) inject their own.
            "ssh_verifier": FakeSSHVerifier()}


def get_service(request: Request):
    from p2r_orchestration.service import OrchestrationService
    runtime = request.app.state.runtime
    with runtime["session_factory"]() as db:
        yield OrchestrationService(db, supervisor=runtime.get("supervisor"), artifact_store=runtime.get("artifact_store"))


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
    if request.app.state.runtime.get("demo_enabled"):
        from .auth import ResourceNotFoundError
        if request.headers.get("X-Actor-ID", "demo-user") != "demo-user":
            raise ResourceNotFoundError("resource not found")
        return "demo-user"
    if request.app.state.runtime.get("development_auth"):
        return request.headers.get("X-Actor-ID", "dev-user")
    _, row, _ = current_auth(request)
    return row.user_id


import os
