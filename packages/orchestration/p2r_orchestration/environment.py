from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
from pathlib import Path
from typing import Mapping


_SENSITIVE_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:bearer\s+[^\s,;]+)"
    r"|(?:https?://[^\s/@]+:[^\s/@]+@)"
    r"|(?:\b(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;&]+"
)


def _safe_allowlisted_value(value: str) -> str | None:
    if _SENSITIVE_VALUE_RE.search(value):
        return None
    return value


def _lock_digest(package_lock: str | os.PathLike[str] | bytes | None) -> str | None:
    if package_lock is None:
        return None
    if isinstance(package_lock, bytes):
        data = package_lock
    else:
        path = Path(package_lock)
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("package lock must be a regular file no larger than 16 MiB")
        data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def environment_snapshot(
    *,
    package_lock: str | os.PathLike[str] | bytes | None = None,
    env_allowlist: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return a bounded, JSON-safe environment summary with no ambient secrets."""
    safe_env: dict[str, str] = {}
    if env_allowlist:
        for key, value in env_allowlist.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 64:
                raise ValueError("environment allowlist keys must be short identifiers")
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError("environment allowlist values must be short strings")
            upper = key.upper()
            if any(marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY", "PROXY", "AUTH")):
                continue
            safe_value = _safe_allowlisted_value(value)
            if safe_value is not None:
                safe_env[key] = safe_value
    return {
        "runtime": {"name": "python", "version": platform.python_version()},
        "python": {"implementation": platform.python_implementation(), "executable": Path(sys.executable).name},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "hardware": {"cpu_count": os.cpu_count()},
        "package_lock_sha256": _lock_digest(package_lock),
        "env": safe_env,
    }
