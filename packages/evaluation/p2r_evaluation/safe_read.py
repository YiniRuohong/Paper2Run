"""No-follow opens below a trusted root on hosts without O_NOFOLLOW/dir_fd.

POSIX callers keep using O_NOFOLLOW with directory fds; nothing here replaces
that. Windows has neither, so ``open_below_root_windows`` provides equivalent
semantics:

1. ``os.lstat`` every component from the drive anchor down and refuse any
   symlink or reparse point (junctions and mount points are reparse points
   that ``S_ISLNK`` does not report);
2. open the file, then require that ``os.fstat`` of the open handle is the
   same file (``st_dev``/``st_ino``) the walk inspected, so a file swapped in
   between the check and the open is refused;
3. require that ``os.path.realpath`` of the opened path is still inside the
   real root.

Windows behaviour is verified only by unit tests that force this branch on
POSIX; it has not been run on a real Windows machine.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Sequence

FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class UnsafePath(ValueError):
    """A path component is a link/reparse point, swapped, or escapes the root."""


def is_link_or_reparse(info) -> bool:
    """True for a symlink and, on Windows, for any reparse point (junction, mount point)."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _inside(root: str, path: str) -> bool:
    root, path = os.path.normcase(root), os.path.normcase(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # different drives
        return False


def open_below_root_windows(root: Path, parts: Sequence[str]) -> int:
    """Open ``root/parts...`` read-only; return the fd or raise ``UnsafePath``/``OSError``.

    ``FileNotFoundError`` propagates with the missing path as ``filename``.
    """
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise UnsafePath("unsafe artifact root: expected absolute path without traversal")
    if not parts or any(not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\\" in p
                        or "\x00" in p for p in parts):
        raise UnsafePath("unsafe path component")
    current = Path(root.anchor)
    components = [*root.parts[1:], *parts]
    for index, part in enumerate(components):
        current = current / part
        info = os.lstat(current)
        if is_link_or_reparse(info):
            raise UnsafePath(f"unsafe path: symlink or reparse point at {part}")
        if index < len(components) - 1 and not stat.S_ISDIR(info.st_mode):
            raise UnsafePath(f"unsafe path: non-directory component {part}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath("unsafe path: not a regular file")
    fd = os.open(current, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    try:
        opened = os.fstat(fd)
        # A zero file index cannot prove identity; fail closed rather than compare 0 == 0.
        if not opened.st_ino or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafePath("unsafe path: file changed between check and open")
        if not _inside(os.path.realpath(root), os.path.realpath(current)):
            raise UnsafePath("unsafe path: resolves outside the trusted root")
    except BaseException:
        os.close(fd)
        raise
    return fd
