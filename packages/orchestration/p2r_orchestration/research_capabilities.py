"""Document-only research skills. Importing never executes package contents."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from pydantic import Field, StringConstraints
from typing import Annotated

from p2r_contracts.autonomous import Name, StrictModel, Text, canonical_hash

COMPATIBILITY = "paper2run.skills.v1"
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_FILES = 128


class SkillManifest(StrictModel):
    id: Name
    version: Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
    name: Text
    description: Text
    entrypoint: str
    capabilities: list[Name] = Field(max_length=64)
    compatibility: list[str] = Field(min_length=1, max_length=16)
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


def _package(source: Path) -> dict:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("skill source must be a regular directory")
    files: dict[str, str] = {}
    total = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("skill symlinks are forbidden")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("skill must contain only regular files")
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("skill exceeds size limit")
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_PACKAGE_BYTES or len(files) >= MAX_FILES:
            raise ValueError("skill exceeds size or file count limit")
        files[path.relative_to(source).as_posix()] = base64.b64encode(raw).decode("ascii")
    if "manifest.json" not in files:
        raise ValueError("missing manifest.json")
    manifest = SkillManifest.model_validate_json(base64.b64decode(files["manifest.json"]))
    if COMPATIBILITY not in manifest.compatibility:
        raise ValueError("incompatible skill package")
    if manifest.entrypoint != "SKILL.md" or manifest.entrypoint not in files:
        raise ValueError("skill entrypoint must be SKILL.md; executable entrypoints are unsupported")
    content = base64.b64decode(files["SKILL.md"]).decode("utf-8")
    if not content.strip():
        raise ValueError("SKILL.md must not be empty")
    # Exclude only the manifest's own optional hash to avoid circular hashing.
    normalized = manifest.model_dump(mode="json", exclude={"content_sha256"})
    hash_files = dict(files)
    hash_files["manifest.json"] = normalized
    digest = canonical_hash(hash_files)
    if manifest.content_sha256 and manifest.content_sha256 != digest:
        raise ValueError("skill content hash mismatch")
    return {**manifest.model_dump(mode="json"), "sha256": digest,
            "content": content, "files": files}


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock():
            return method(self, *args, **kwargs)
    return wrapper


class CapabilityRegistry:
    def __init__(self, root: Path | None = None, imported_root: Path | None = None):
        self.root = Path(root) if root is not None else Path(os.environ.get(
            "P2R_SKILLS_DIR", str(Path(__file__).resolve().parents[3] / "resources" / "research-skills")))
        self.imported_root = Path(imported_root) if imported_root is not None else self.root.parent / "imported-research-skills"

    @contextmanager
    def _lock(self):
        # Registry storage is control-owned. All instances/processes coordinate
        # through the same persistent lock file (never unlink a lock inode).
        try:
            import fcntl
        except ImportError as exc:
            raise ValueError("registry locking requires POSIX flock") from exc
        if self.imported_root.is_symlink():
            raise ValueError("unsafe imported skills root")
        self.imported_root.mkdir(parents=True, exist_ok=True)
        key = str(self.imported_root.resolve())
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.Lock())
        with lock:
            fd = os.open(self.imported_root / ".registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _listed(self) -> list[dict]:
        return [{k: v for k, v in item.items() if k not in ("files", "content")}
                for _, item in sorted(self._packages().items())]

    def _state(self) -> dict:
        path = self.imported_root / "enabled.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ValueError("unsafe skill state path")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or any(not isinstance(k, str) or type(v) is not bool for k, v in data.items()):
            raise ValueError("invalid skill enabled state")
        return data

    def _packages(self) -> dict[str, dict]:
        packages: dict[str, dict] = {}
        for root, origin in ((self.root, "bundled"), (self.imported_root, "local")):
            if root.is_symlink():
                raise ValueError("unsafe skill registry root")
            if not root.exists():
                if origin == "bundled":
                    raise ValueError("bundled skills unavailable; configure P2R_SKILLS_DIR")
                continue
            candidates = []
            for path in sorted(root.iterdir()):
                if origin == "local" and (path.name == "enabled.json" or path.name.startswith(".")):
                    continue
                if path.is_symlink():
                    raise ValueError("skill symlinks are forbidden")
                if not path.is_dir():
                    continue
                candidates.append(_package(path))
            for package in sorted(candidates, key=lambda item: (item["id"], self._version(item))):
                key = package["id"]
                if key in packages:
                    previous = packages[key]
                    if origin == "bundled" or self._version(package) < self._version(previous):
                        raise ValueError("duplicate or downgraded skill")
                    if package["version"] == previous["version"] and package["sha256"] != previous["sha256"]:
                        raise ValueError("same skill version has changed contents")
                packages[key] = {**package, "source": origin}
        state = self._state()
        return {key: {**package, "enabled": state.get(key, True)} for key, package in packages.items()}

    @staticmethod
    def _version(package: dict) -> tuple[int, ...]:
        return tuple(map(int, package["version"].split(".")))

    @_locked
    def list(self) -> list[dict]:
        return self._listed()

    @_locked
    def snapshot(self, skill_ids: list[str]) -> list[dict]:
        packages = self._packages()
        result = []
        if len(set(skill_ids)) != len(skill_ids):
            raise ValueError("duplicate skill selection")
        for key in skill_ids:
            if key not in packages:
                raise ValueError(f"unknown skill: {key}")
            if not packages[key]["enabled"]:
                raise ValueError(f"disabled skill: {key}")
            result.append(packages[key])
        return result

    @_locked
    def import_local(self, source: Path) -> dict:
        package = _package(Path(source))
        existing = self._packages().get(package["id"])
        if existing:
            if self._version(package) < self._version(existing):
                raise ValueError("skill version downgrade is forbidden")
            if package["version"] == existing["version"]:
                if package["sha256"] != existing["sha256"]:
                    raise ValueError("same skill version has changed contents")
                return {k: v for k, v in existing.items() if k not in ("files", "content")}
        if self.imported_root.is_symlink():
            raise ValueError("unsafe imported skills root")
        self.imported_root.mkdir(parents=True, exist_ok=True)
        destination = self.imported_root / f"{package['id']}--{package['version']}"
        if destination.exists() or destination.is_symlink():
            raise ValueError("skill version destination already exists")
        # A staging directory is never scanned. Failed imports clean up their
        # staging bytes; process death can leave only an ignored hidden directory.
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=self.imported_root) as staging:
            staged = Path(staging) / "package"
            staged.mkdir()
            for relative, encoded in package["files"].items():
                path = staged / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative == "manifest.json":
                    manifest = SkillManifest.model_validate_json(base64.b64decode(encoded))
                    manifest.content_sha256 = package["sha256"]
                    path.write_text(manifest.model_dump_json(), encoding="utf-8")
                else:
                    path.write_bytes(base64.b64decode(encoded))
            _package(staged)
            os.rename(staged, destination)
        return next(item for item in self._listed() if item["id"] == package["id"])

    @_locked
    def set_enabled(self, skill_id: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if skill_id not in self._packages():
            raise ValueError(f"unknown skill: {skill_id}")
        state = self._state()
        state[skill_id] = enabled
        self.imported_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".state-", dir=self.imported_root) as staging:
            staged = Path(staging) / "enabled.json"
            with staged.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.imported_root / "enabled.json")
        return next(item for item in self._listed() if item["id"] == skill_id)
