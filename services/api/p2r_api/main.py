from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from p2r_contracts.common import ApiError, ErrorEnvelope
from p2r_orchestration.errors import ServiceError

from .dependencies import LOCAL_ACTOR_ID, build_runtime, data_dir
from .routes import router


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
            def self_heal():
                # After the app was moved or upgraded; reads the state here so it never blocks startup.
                try:
                    if service.installer.state().state == 'ready':
                        service.installer.ensure_healthy()
                except Exception:
                    pass

            try:
                threading.Thread(target=self_heal, name='docparse-health', daemon=True).start()
                yield
            finally:
                stop.set()  # the worker requeues its running job instead of failing it
                try:
                    service.installer.cancel()  # kill a running install or health check
                except Exception:
                    pass
                stop_docparse_worker(stop, thread)

    app.router.lifespan_context = docparse_lifespan


def _install_autonomous_lifespan(app: FastAPI) -> None:
    from contextlib import asynccontextmanager
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def autonomous_lifespan(fastapi_app):
        async with previous(fastapi_app):
            service = fastapi_app.state.runtime['autonomous_service']
            service.recover()
            try:
                yield
            finally:
                service.shutdown()

    app.router.lifespan_context = autonomous_lifespan


def _install_image_autoload(app: FastAPI) -> None:
    """Desktop installer: import the bundled experiment image into Docker when the app starts.

    Only active when the shell sets P2R_IMAGE_ARCHIVE. The import runs in a background thread,
    so startup never waits for Docker.
    """
    import os
    from contextlib import asynccontextmanager

    archive = os.environ.get('P2R_IMAGE_ARCHIVE')
    if not archive:
        return
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def image_lifespan(fastapi_app):
        async with previous(fastapi_app):
            from p2r_sandbox.autonomous.envcheck import IMAGE_LOADER
            IMAGE_LOADER.start(archive)
            yield

    app.router.lifespan_context = image_lifespan


def create_app(*, db_url: str | None = None, session_factory=None, artifact_store=None, allowed_origin: str | tuple[str, ...] | None = None, secure_cookies: bool = True, development_auth: bool = False, docparse_service=None, local_actor: str | None = None) -> FastAPI:
    """API app with the kept routers (auth, projects, papers, docparse, autonomous).

    Authenticated by default; ``create_local_app`` is the single-user local runtime.
    ``allowed_origin`` is the exact Origin (or tuple of origins) state-changing requests must carry.
    """
    app = FastAPI(title="p2r-api")
    app.state.runtime = build_runtime(db_url=db_url, session_factory=session_factory, artifact_store=artifact_store, allowed_origin=allowed_origin, secure_cookies=secure_cookies, development_auth=development_auth, docparse_service=docparse_service, local_actor=local_actor)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            identity_db = getattr(request.state, "identity_db", None)
            if identity_db is not None:
                identity_db.close()

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        details = dict(getattr(exc, "details", {}) or {})
        body = ErrorEnvelope(error=ApiError(code=exc.code, message=exc.message, retryable=exc.status >= 500, details=details), request_id=request.state.request_id).model_dump(mode="json")
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
    _install_autonomous_lifespan(app)
    return app


def create_local_app(*, db_url=None, session_factory=None, artifact_store=None, docparse_service=None,
                     allowed_origins=None) -> FastAPI:
    """Loopback-only single-user runtime used by the launcher and the desktop app.

    Start with ``uvicorn p2r_api.main:create_local_app --factory --host 127.0.0.1``.
    Without an injected database, state lives under ``P2R_DEMO_DIR`` (default
    ``.p2r-demo``; the name is historical and kept so existing data stays put):
    ``demo.sqlite3`` plus ``artifacts/``. There is no login: every request acts as
    the fixed local user, which is created on first start. Not production auth.

    State-changing requests must carry an Origin that is exactly one of
    ``allowed_origins``; when not given, ``P2R_ALLOWED_ORIGINS`` (comma-separated),
    else ``http://127.0.0.1:8000``. Every entry must be an ``http://<loopback>:<port>``
    origin; anything else raises ``ValueError`` before any state is touched.
    """
    from sqlalchemy.engine import URL

    from p2r_artifacts.store import LocalArtifactStore

    origins = _local_origins(allowed_origins)
    if db_url is None and session_factory is None:
        root = data_dir()
        root.mkdir(parents=True, exist_ok=True)
        db_url = URL.create('sqlite', database=str(root / 'demo.sqlite3')).render_as_string(hide_password=False)
        artifact_store = artifact_store or LocalArtifactStore(root / 'artifacts')
    app = create_app(
        db_url=db_url, session_factory=session_factory, artifact_store=artifact_store,
        development_auth=True, allowed_origin=origins, secure_cookies=False,
        docparse_service=docparse_service, local_actor=LOCAL_ACTOR_ID,
    )
    _ensure_local_user(app.state.runtime['session_factory'])
    _install_docparse_worker_lifespan(app)
    _install_image_autoload(app)
    _mount_web_dist(app)
    return app


DEFAULT_LOCAL_ORIGIN = 'http://127.0.0.1:8000'


def _local_origins(allowed_origins) -> tuple[str, ...]:
    import os

    from .auth import parse_loopback_origins

    if allowed_origins is not None:
        if isinstance(allowed_origins, str):
            allowed_origins = [allowed_origins]
        return parse_loopback_origins(','.join(allowed_origins))
    configured = os.environ.get('P2R_ALLOWED_ORIGINS')
    return parse_loopback_origins(configured if configured is not None else DEFAULT_LOCAL_ORIGIN)


def _ensure_local_user(session_factory) -> None:
    """Create the fixed local user if missing; an existing row is never modified."""
    from p2r_persistence.models import UserRecord

    with session_factory() as db:
        if db.get(UserRecord, LOCAL_ACTOR_ID) is None:
            db.add(UserRecord(id=LOCAL_ACTOR_ID, display_name='Local user', can_create_project=True))
            db.commit()


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
