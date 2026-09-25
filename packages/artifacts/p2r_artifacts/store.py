from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ArtifactStoreError(ValueError):
    """Base error for invalid artifact-store requests."""


class ArtifactIntegrityError(ArtifactStoreError):
    """An address exists but its bytes do not match its content address."""


class ArtifactNotFoundError(ArtifactStoreError):
    """The requested artifact address does not exist."""


@dataclass(frozen=True, slots=True)
class ArtifactAddress:
    sha256: str
    bytes: int
    path: Path


_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class LocalArtifactStore:
    """A small content-addressed store rooted at one controlled directory."""

    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.root = Path(root).expanduser().resolve()
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("artifact store root must be a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def _validate_hash(self, sha256: str) -> str:
        if not isinstance(sha256, str) or not _HASH_RE.fullmatch(sha256):
            raise ArtifactStoreError("artifact address must be a lowercase SHA-256 hex digest")
        return sha256

    def _path(self, sha256: str) -> Path:
        digest = self._validate_hash(sha256)
        path = (self.root / digest[:2] / digest).resolve()
        if path.parent != (self.root / digest[:2]).resolve() or self.root not in path.parents:
            raise ArtifactStoreError("artifact address escapes store root")
        return path

    def put_bytes(self, data: bytes) -> ArtifactAddress:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        if len(data) > self.max_bytes:
            raise ValueError(f"artifact size exceeds limit of {self.max_bytes} bytes")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            self._verify(path, digest)
            return ArtifactAddress(digest, len(data), path)

        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                self._verify(path, digest)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return ArtifactAddress(digest, len(data), path)

    def _verify(self, path: Path, digest: str) -> bytes:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError(f"artifact integrity check failed for {digest}") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactIntegrityError(f"artifact integrity check failed for {digest}")
        return content

    def open_bytes(self, sha256: str) -> bytes:
        path = self._path(sha256)
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact not found: {sha256}")
        return self._verify(path, sha256)

    def exists(self, sha256: str) -> bool:
        path = self._path(sha256)
        if not path.exists():
            return False
        if not path.is_file():
            raise ArtifactIntegrityError(f"artifact integrity check failed for {sha256}")
        self._verify(path, sha256)
        return True
