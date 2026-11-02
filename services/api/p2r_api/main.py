from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from p2r_contracts.common import ApiError, ErrorEnvelope
from p2r_orchestration.service import ServiceError

from .dependencies import build_runtime
from .routes import router


def _install_demo_worker_lifespan(app: FastAPI) -> None:
    from contextlib import asynccontextmanager

    from .demo import start_demo_worker, stop_demo_worker

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def demo_lifespan(fastapi_app):
        async with previous_lifespan(fastapi_app):
            stop, thread = start_demo_worker(
                fastapi_app.state.runtime['session_factory'],
                fastapi_app.state.runtime['artifact_store'],
            )
            fastapi_app.state.runtime['demo_worker_stop'] = stop
            fastapi_app.state.runtime['demo_worker_thread'] = thread
            try:
                yield
            finally:
                # One bounded join owns this single background consumer.
                stop_demo_worker(stop, thread)

    app.router.lifespan_context = demo_lifespan


def _install_docparse_worker_lifespan(app: FastAPI) -> None:
    import threading
    from contextlib import asynccontextmanager

    from p2r_orchestration.docparse_jobs import start_docparse_worker, stop_docparse_worker

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def docparse_lifespan(fastapi_app):
        async with previous_lifespan(fastapi_app):
            runtime = fastapi_app.state.runtime
            service = runtime['docparse_service']
            stop, thread = start_docparse_worker(
                runtime['session_factory'], runtime['artifact_store'], service,
            )
            runtime['docparse_worker_stop'] = stop
            runtime['docparse_worker_thread'] = thread
            try:
                if service.installer.state().state == 'ready':
                    # Self-heal after the app was moved or upgraded; never blocks startup.
                    threading.Thread(target=service.installer.ensure_healthy,
                                     name='docparse-health', daemon=True).start()
                yield
            finally:
                stop_docparse_worker(stop, thread)

    app.router.lifespan_context = docparse_lifespan


def create_app(*, db_url: str | None = None, session_factory=None, supervisor=None, artifact_store=None, allowed_origin: str | None = None, secure_cookies: bool = True, development_auth: bool = False, docparse_service=None) -> FastAPI:
    app = FastAPI(title="p2r-api")
    # Preserve the explicit demo/supervisor test harness while keeping the
    # production default (create_app() with no arguments) authenticated.
    compatibility_auth = development_auth or (supervisor is not None and allowed_origin is None)
    app.state.runtime = build_runtime(db_url=db_url, session_factory=session_factory, supervisor=supervisor, artifact_store=artifact_store, allowed_origin=allowed_origin, secure_cookies=secure_cookies, development_auth=compatibility_auth, docparse_service=docparse_service)

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


def create_demo_app(*, db_url=None, session_factory=None, artifact_store=None, supervisor=None, docparse_service=None) -> FastAPI:
    """Opt-in, loopback-only development runtime; not a hostile-code sandbox.

    Start with uvicorn p2r_api.main:create_demo_app --factory --host 127.0.0.1.
    All local Demo state lives under P2R_DEMO_DIR (default .p2r-demo).
    Uses the fixed demo-user actor, not production authentication. Inject both
    a database/session factory and an artifact store for isolated tests.
    """
    import os
    from pathlib import Path

    from sqlalchemy.engine import URL

    from p2r_artifacts.store import LocalArtifactStore
    from p2r_kernel.supervisor import KernelSupervisor

    if db_url is None and session_factory is None:
        root = Path(os.environ.get('P2R_DEMO_DIR', '.p2r-demo')).resolve()
        workspace = root / 'workspace'
        workspace.mkdir(parents=True, exist_ok=True)
        db_url = URL.create('sqlite', database=str(root / 'demo.sqlite3')).render_as_string(hide_password=False)
        supervisor = supervisor or KernelSupervisor(workspace)
        artifact_store = artifact_store or LocalArtifactStore(root / 'artifacts')
    app = create_app(
        db_url=db_url, session_factory=session_factory, supervisor=supervisor,
        artifact_store=artifact_store,
        development_auth=True, allowed_origin='http://127.0.0.1:8000', secure_cookies=False,
        docparse_service=docparse_service,
    )
    from .routes.demo import router as demo_router
    app.state.runtime['demo_enabled'] = True
    app.include_router(demo_router, prefix='/api/v1')
    _install_demo_worker_lifespan(app)
    _install_docparse_worker_lifespan(app)
    _mount_web_dist(app)
    return app


def _mount_web_dist(app: FastAPI) -> None:
    """Serve the built web UI from this single process when it is available.

    Out-of-box mode runs one port for both API and UI: ``apps/web/dist`` (or
    ``P2R_WEB_DIST``) is mounted with an SPA fallback so any non-API GET path
    serves index.html. When the build is absent the API still runs and the
    launcher prints how to build the UI.
    """
    import os
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    dist = Path(os.environ.get('P2R_WEB_DIST') or
                Path(__file__).resolve().parents[3] / 'apps' / 'web' / 'dist')
    index = dist / 'index.html'
    if not index.is_file():
        return
    assets = dist / 'assets'
    if assets.is_dir():
        app.mount('/assets', StaticFiles(directory=assets), name='assets')

    @app.get('/{spa_path:path}', include_in_schema=False)
    async def spa(spa_path: str):
        if spa_path.startswith('api/'):
            from .routes.papers import _error
            _error('RESOURCE_NOT_FOUND', 'resource not found', status=404)
        candidate = (dist / spa_path).resolve()
        # A resolved file under dist is served directly; anything else falls
        # back to the SPA entry so client-side routes keep working on refresh.
        if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
