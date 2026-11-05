"""Read-only task file listing and bounded raw preview for the workbench UI.

Listing is cheap (no hashing). Raw reads re-verify frozen artifact hashes,
never follow symlinks, stay inside the task's local workspace and refuse any
content carrying a runtime credential.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import stat
from urllib.parse import quote

from .evidence import MAX_FILE, safe_name
from p2r_orchestration.service import ServiceError
from .security import fail

MAX_LIST_FILES = 2000
MAX_DEPTH = 12
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"})
_OVERRIDES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".py": "text/x-python", ".json": "application/json",
    ".jsonl": "text/plain", ".log": "text/plain", ".txt": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".yaml": "text/plain", ".yml": "text/plain", ".toml": "text/plain", ".sh": "text/plain", ".ini": "text/plain",
    ".cfg": "text/plain", ".ts": "text/plain", ".tsx": "text/plain", ".r": "text/plain", ".ipynb": "application/json",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml",
}
# Active content is downgraded to inert text even though CSP sandboxes it.
_INERT = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                    "text/javascript", "application/javascript", "application/x-javascript", "text/ecmascript"})
_TEXTUAL = frozenset({"application/json", "application/x-yaml", "application/toml", "application/x-sh"})
CSP = "sandbox; default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'"


def media_type(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in _OVERRIDES:
        return _OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(name, strict=False)
    return guessed or "application/octet-stream"


def raw_headers(media: str, raw: bytes, filename: str) -> tuple[str, dict]:
    base = (media or "application/octet-stream").split(";", 1)[0].strip().lower()
    if base in _INERT:
        content_type = "text/plain; charset=utf-8"
    elif base.startswith("text/") or base in _TEXTUAL:
        try:
            raw.decode("utf-8")
            content_type = base + "; charset=utf-8"
        except UnicodeDecodeError:
            content_type = base
    else:
        content_type = base
    name = PurePosixPath(filename.replace("\\", "/")).name or "file"
    return content_type, {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": CSP,
        "Content-Disposition": "inline; filename*=UTF-8''" + quote(name, safe=""),
        "Cache-Control": "no-store",
    }


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _artifact_listing_path(item) -> str:
    version = item.get("protocol_sha256")
    return "artifacts/" + (version[:12] if version else "unversioned") + "/" + safe_name(item["name"])


def workspace_root(service, row) -> Path | None:
    """Local task workspace root, or None for remote (ssh) or unconfigured targets."""
    snapshot = row.config_snapshot or {}
    if (snapshot.get("target") or {}).get("kind") == "ssh":
        return None
    workspace = (snapshot.get("project") or {}).get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return None
    service.task_root(row.id)  # validates the DB-originated id shape
    return Path(workspace) / row.id


def snapshot(service, row) -> dict:
    """Plain data needed for file IO, captured under the service lock."""
    return {"task_root": service.task_root(row.id), "artifacts": [dict(a) for a in (row.artifacts_json or [])],
            "workspace": workspace_root(service, row)}


def list_files(snap) -> dict:
    items = []
    for item in snap["artifacts"]:
        try:
            path = _artifact_listing_path(item)
            name = safe_name(item["name"])
        except (ServiceError, ValueError, KeyError, TypeError):
            continue
        version = item.get("protocol_sha256") or "unversioned"
        disk = snap["task_root"] / "artifacts" / version / name
        try:
            modified = _iso(disk.lstat().st_mtime)
        except OSError:
            modified = None
        items.append({"path": path, "source": "artifact", "bytes": item.get("bytes") or 0,
                      "media_type": media_type(name), "modified_at": modified, "sha256": item.get("sha256"),
                      "kind": item.get("kind"), "protocol_sha256": item.get("protocol_sha256")})
    root, truncated, available = snap["workspace"], False, False
    if root is not None and root.is_dir() and not root.is_symlink():
        available = True
        resolved_root = root.resolve()
        count = 0
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(current)
            depth = len(here.relative_to(root).parts)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (here / d).is_symlink()
                                 and depth + 1 <= MAX_DEPTH)
            for filename in sorted(filenames):
                full = here / filename
                try:
                    info = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or not full.resolve().is_relative_to(resolved_root):
                    continue
                if count >= MAX_LIST_FILES:
                    truncated = True
                    break
                count += 1
                rel = full.relative_to(root).as_posix()
                items.append({"path": "workspace/" + rel, "source": "workspace", "bytes": info.st_size,
                              "media_type": media_type(filename), "modified_at": _iso(info.st_mtime),
                              "sha256": None, "kind": None, "protocol_sha256": None})
            if truncated:
                break
    items.sort(key=lambda entry: entry["path"])
    return {"items": items, "truncated": truncated, "workspace_available": available}


def validate_path(path) -> PurePosixPath:
    if (not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path or path.startswith("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        fail("VALIDATION_ERROR", "path must be a relative safe path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail("VALIDATION_ERROR", "path must be a relative safe path")
    return PurePosixPath(path)


def _not_found():
    fail("RESOURCE_NOT_FOUND", "file not found", 404)


def _read_nofollow(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _not_found()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _not_found()
        if info.st_size > MAX_FILE:
            fail("FILE_TOO_LARGE", "file exceeds preview size limit", 413)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE:
                fail("FILE_TOO_LARGE", "file exceeds preview size limit", 413)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_file(snap, source: str, path: str) -> tuple[bytes, str, str]:
    """Return (raw bytes, media type, basename) for a listing path."""
    rel = validate_path(path)
    if source == "artifact":
        if rel.parts[0] != "artifacts":
            _not_found()
        item = None
        for candidate in snap["artifacts"]:
            try:
                if _artifact_listing_path(candidate) == path:
                    item = candidate
                    break
            except (ServiceError, ValueError, KeyError, TypeError):
                continue
        if item is None:
            _not_found()
        name = safe_name(item["name"])
        root = snap["task_root"] / "artifacts" / (item.get("protocol_sha256") or "unversioned")
        disk = root / name
        if disk.is_symlink() or not disk.resolve().is_relative_to(root.resolve()) or not disk.is_file():
            fail("EVIDENCE_INTEGRITY", "frozen artifact is missing or unsafe", 409)
        raw = _read_nofollow(disk)
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            fail("EVIDENCE_INTEGRITY", "frozen artifact hash mismatch", 409)
        return raw, media_type(name), rel.name
    if source == "workspace":
        root = snap["workspace"]
        if rel.parts[0] != "workspace" or len(rel.parts) < 2 or root is None:
            _not_found()
        inner = rel.parts[1:]
        if len(inner) > MAX_DEPTH + 1 or any(part in SKIP_DIRS for part in inner[:-1]):
            _not_found()
        if not root.is_dir() or root.is_symlink():
            _not_found()
        current = root
        for part in inner:
            current = current / part
            if current.is_symlink():
                _not_found()
        if not current.resolve().is_relative_to(root.resolve()):
            _not_found()
        raw = _read_nofollow(current)
        return raw, media_type(rel.name), rel.name
    fail("VALIDATION_ERROR", "source must be artifact or workspace")


def refuse_secrets(service, raw: bytes):
    with service.profiles.lock:
        secrets = tuple(s.encode() for s in service.profiles.secrets)
    if any(s and s in raw for s in secrets):
        fail("SECRET_IN_ARTIFACT", "file contains a runtime credential", 409)
