from fastapi import APIRouter
from .auth import router as auth_router
from .papers import router as papers_router
from .projects import router as projects_router
from .docparse import router as docparse_router
from .autonomous import router as autonomous_router
from .remote import router as remote_router
from .envcheck import router as envcheck_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(papers_router)
router.include_router(projects_router)
router.include_router(docparse_router)
router.include_router(autonomous_router)
router.include_router(remote_router)
router.include_router(envcheck_router)

__all__ = ["router"]
