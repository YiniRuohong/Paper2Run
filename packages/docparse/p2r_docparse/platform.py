"""Which platform the offline MinerU runtime can be installed on."""
from __future__ import annotations

import platform as _stdlib_platform
import sys

OFFLINE_PLATFORMS = frozenset({"darwin-arm64", "win32-x64"})

_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}


def current_platform() -> str:
    machine = _stdlib_platform.machine().lower()
    return f"{sys.platform}-{_ARCH.get(machine, machine)}"
