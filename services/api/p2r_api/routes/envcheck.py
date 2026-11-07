"""Environment check and bundled-image import for the desktop app (read-only checks, one explicit import)."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request

from p2r_sandbox.autonomous.envcheck import IMAGE_LOADER, run_all

from ..dependencies import actor_id, data_dir
from .papers import _envelope, _error

router = APIRouter()


def _archive():
    return os.environ.get("P2R_IMAGE_ARCHIVE") or None


@router.get("/env/check")
def env_check(request: Request):
    actor_id(request)
    return _envelope(request, run_all(_archive(), data_dir()))


@router.post("/env/image/load", status_code=202)
def env_image_load(request: Request):
    actor_id(request)
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        _error("CROSS_SITE_REFUSED", "cross-site requests are not allowed", status=403)
    return _envelope(request, IMAGE_LOADER.start(_archive()))
