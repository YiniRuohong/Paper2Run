"""Run a subprocess tree with a hard timeout; no shell, bounded output."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TAIL_BYTES = 4096
_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class CompletedRun:
    returncode: int
    stdout_tail: str
    stderr_tail: str
    timed_out: bool
    cancelled: bool = False


def venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python"


def mineru_kit(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "venv" / "Scripts" / "mineru-kit.exe"
    return root / "venv" / "bin" / "mineru-kit"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def _tail(fh) -> str:
    fh.flush()
    size = fh.seek(0, os.SEEK_END)
    fh.seek(max(0, size - TAIL_BYTES))
    return fh.read().decode("utf-8", errors="replace")


def run_tree(argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float,
             cancelled: Callable[[], bool] = lambda: False) -> CompletedRun:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, **kwargs)
        deadline = time.monotonic() + timeout
        timed_out = was_cancelled = False
        try:
            while True:
                try:
                    proc.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancelled():
                    was_cancelled = True
                elif time.monotonic() >= deadline:
                    timed_out = True
                else:
                    continue
                _kill_tree(proc)
                break
        except BaseException:
            _kill_tree(proc)
            raise
        return CompletedRun(proc.returncode, _tail(out), _tail(err), timed_out, was_cancelled)
