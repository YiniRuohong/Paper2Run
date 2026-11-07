"""Cross-process exclusive file locks for control-owned lock files.

POSIX: ``fcntl.flock`` on an ``O_NOFOLLOW`` descriptor (unchanged semantics).
Windows: ``msvcrt.locking`` on byte 0, after refusing a lock path that is a
symlink or reparse point. Both conflict between different open handles, also
within one process. ``release`` unlocks explicitly before closing, because
Windows documents only an eventual unlock when a locked handle is closed.

The Windows branch is verified only by unit tests that force it on POSIX with
a fake ``msvcrt``; it has not been run on a real Windows machine.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import time

from p2r_evaluation.safe_read import is_link_or_reparse

_BUSY = (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK), errno.EDEADLK)
_POLL_SECONDS = 0.05


def _windows() -> bool:
    return os.name == "nt"


def open_lock_file(path: Path) -> int:
    """Create/open a lock file without following links; return the fd."""
    if not _windows():
        return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and (is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode)):
        raise OSError(errno.ELOOP, "lock file is a link, reparse point or non-regular file", str(path))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0), 0o600)
    try:
        opened, now = os.fstat(fd), os.lstat(path)
        same = (opened.st_dev, opened.st_ino) == (now.st_dev, now.st_ino) and bool(opened.st_ino)
        if is_link_or_reparse(now) or not same or (
                before is not None and (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino)):
            raise OSError(errno.ELOOP, "lock file changed while opening", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire(fd: int, *, blocking: bool = True) -> None:
    """Take the exclusive lock; a non-blocking call raises ``OSError`` when it is held elsewhere."""
    if not _windows():
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            # LK_LOCK gives up after ten seconds, so blocking polls LK_NBLCK.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if not blocking or exc.errno not in _BUSY:
                raise
        time.sleep(_POLL_SECONDS)


def release(fd: int) -> None:
    """Unlock (explicitly on Windows) and close the descriptor."""
    try:
        if _windows():
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # closing the handle still releases the region
    finally:
        os.close(fd)


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True):
    fd = open_lock_file(path)
    try:
        acquire(fd, blocking=blocking)
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        release(fd)
