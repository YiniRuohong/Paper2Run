"""Tablet remote: a thin proxy to the p2r-tunnel control API.

The tunnel sidecar publishes ``{"port","token","pid"}`` in a control file
(``P2R_REMOTE_CONTROL_FILE``, default ``~/.paper2run/remote/control.json``).
Requests that arrived through the tunnel carry ``X-P2R-Remote: 1`` (set by the
sidecar, which strips any client-supplied value); those may read state but
never start pairing or revoke devices.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from p2r_orchestration.service import ServiceError

router = APIRouter()

_TIMEOUT_S = 10


class RemoteUnavailable(Exception):
    pass


def err(code: str, message: str, status: int) -> None:
    error = ServiceError(message, details={})
    error.code = code
    error.status = status
    raise error


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request.state.request_id}}


def control_file() -> Path:
    configured = os.environ.get("P2R_REMOTE_CONTROL_FILE")
    return Path(configured) if configured else Path.home() / ".paper2run" / "remote" / "control.json"


def is_remote(request: Request) -> bool:
    return request.headers.get("x-p2r-remote") == "1"


def _call(method: str, path: str) -> tuple[int, Any]:
    try:
        info = json.loads(control_file().read_text())
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RemoteUnavailable("remote sidecar is not running") from exc
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed loopback URL
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body) if body else None
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError) as exc:
        raise RemoteUnavailable("remote sidecar is not reachable") from exc


def _guard_write(request: Request) -> None:
    if is_remote(request):
        err("REMOTE_FORBIDDEN", "pairing and revoking are only allowed on the desktop", 403)
    if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
        err("REMOTE_FORBIDDEN", "cross-site request rejected", 403)


def _proxy(method: str, path: str) -> tuple[int, Any]:
    try:
        status, body = _call(method, path)
    except RemoteUnavailable as exc:
        err("REMOTE_UNAVAILABLE", str(exc), 503)
    if status >= 400:
        message = (body or {}).get("error", "remote sidecar error") if isinstance(body, dict) else "remote sidecar error"
        err("REMOTE_NOT_FOUND" if status == 404 else "REMOTE_ERROR", message, 404 if status == 404 else 502)
    return status, body


@router.get("/remote/status")
def remote_status(request: Request):
    remote = is_remote(request)
    try:
        status, body = _call("GET", "/status")
    except RemoteUnavailable:
        return envelope(request, {"available": False, "remote": remote})
    if status != 200 or not isinstance(body, dict):
        return envelope(request, {"available": False, "remote": remote})
    return envelope(request, {**body, "available": True, "remote": remote})


@router.get("/remote/pairing")
def pairing_state(request: Request):
    _, body = _proxy("GET", "/pairing")
    return envelope(request, body)


@router.post("/remote/pairing")
def start_pairing(request: Request):
    _guard_write(request)
    _, body = _proxy("POST", "/pairing")
    return envelope(request, body)


@router.delete("/remote/devices/{device_id}", status_code=204)
def revoke_device(device_id: str, request: Request):
    _guard_write(request)
    if not (len(device_id) == 64 and all(c in "0123456789abcdef" for c in device_id)):
        err("VALIDATION_ERROR", "bad device id", 422)
    _proxy("DELETE", f"/devices/{device_id}")
    return Response(status_code=204)
