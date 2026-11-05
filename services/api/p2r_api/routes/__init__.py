from fastapi import APIRouter
from .auth import router as auth_router
from .sessions import router as sessions_router
from .papers import router as papers_router
from .projects import router as projects_router
from .experiments import router as experiments_router
from .runs import router as runs_router
from .reports import router as reports_router
from .operations import router as operations_router
from .provider_sessions import router as provider_sessions_router
from .environment import router as environment_router
from .codegen import router as codegen_router
from .docparse import router as docparse_router
from .autonomous import router as autonomous_router
from .remote import router as remote_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(sessions_router)
router.include_router(papers_router)
router.include_router(projects_router)
router.include_router(experiments_router)
router.include_router(runs_router)
router.include_router(reports_router)
router.include_router(operations_router)
router.include_router(provider_sessions_router)
router.include_router(environment_router)
router.include_router(codegen_router)
router.include_router(docparse_router)
router.include_router(autonomous_router)
router.include_router(remote_router)

__all__ = ["router"]
