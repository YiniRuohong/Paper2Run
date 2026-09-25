from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from p2r_contracts.common import ApiError, ErrorEnvelope
from p2r_orchestration.service import ServiceError

from .dependencies import build_runtime
from .routes import router


def create_app(*, db_url: str | None = None, session_factory=None, supervisor=None, artifact_store=None) -> FastAPI:
    app = FastAPI(title="p2r-api")
    app.state.runtime = build_runtime(db_url=db_url, session_factory=session_factory, supervisor=supervisor, artifact_store=artifact_store)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        code = "IDEMPOTENCY_CONFLICT" if exc.__class__.__name__ == "IdempotencyConflictError" else exc.code
        details = dict(getattr(exc, "details", {}) or {})
        body = ErrorEnvelope(error=ApiError(code=code, message=exc.message, retryable=exc.status >= 500, details=details), request_id=request.state.request_id).model_dump(mode="json")
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        field_errors = []
        for error in exc.errors():
            loc = list(error.get("loc", ()))
            path = ".".join(str(item) for item in loc if item not in {"body", "query", "path", "header"}) or "$"
            field_errors.append({"path": path, "code": str(error.get("type", "validation_error")), "message": str(error.get("msg", "invalid value"))})
        details = {"field_errors": field_errors}
        body = ErrorEnvelope(error=ApiError(code="VALIDATION_ERROR", message="request validation failed", details=details), request_id=request.state.request_id).model_dump(mode="json")
        return JSONResponse(body, status_code=422)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "p2r-api"})

    app.include_router(router, prefix="/api/v1")
    return app


def create_demo_app() -> FastAPI:
    """Opt-in, loopback-only development runtime; not a hostile-code sandbox.

    Start with uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1.
    All local Demo state lives under P2R_DEMO_DIR (default .p2r-demo).
    This uses development actor headers, not production authentication.
    """
    import os
    from pathlib import Path

    from sqlalchemy.engine import URL

    from p2r_artifacts.store import LocalArtifactStore
    from p2r_kernel.supervisor import KernelSupervisor

    root = Path(os.environ.get('P2R_DEMO_DIR', '.p2r-demo')).resolve()
    workspace = root / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    database_url = URL.create('sqlite', database=str(root / 'demo.sqlite3'))
    return create_app(
        db_url=database_url.render_as_string(hide_password=False),
        supervisor=KernelSupervisor(workspace),
        artifact_store=LocalArtifactStore(root / 'artifacts'),
    )


app = create_app()
