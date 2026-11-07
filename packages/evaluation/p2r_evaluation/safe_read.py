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
3. require that the final path of the open handle
   (``GetFinalPathNameByHandleW``, ``FILE_NAME_NORMALIZED``) is inside the
   root's final path, resolved through a directory handle the same way.
   A failure to resolve either path is refused.

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


FILE_NAME_NORMALIZED = 0x0
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a directory handle


def strip_extended_prefix(path: str) -> str:
    """``\\\\?\\C:\\x`` -> ``C:\\x``; ``\\\\?\\UNC\\srv\\share`` -> ``\\\\srv\\share``."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path


def _kernel32():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, kernel32


def _final_path_of_handle(handle) -> str:
    ctypes, kernel32 = _kernel32()
    size = 512
    for _ in range(4):
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, FILE_NAME_NORMALIZED)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        if length < size:
            return strip_extended_prefix(buffer.value)
        size = length + 1  # too small: length is the size needed, including the terminator
    raise OSError(0, "GetFinalPathNameByHandleW buffer size did not settle")


def _final_path_of_fd(fd: int) -> str:
    """Seam: final path of an open CRT fd (Windows only; tests replace it on POSIX)."""
    import msvcrt
    return _final_path_of_handle(msvcrt.get_osfhandle(fd))


def _final_path_of_dir(path) -> str:
    """Seam: final path of a directory, resolved through a handle the same way."""
    ctypes, kernel32 = _kernel32()
    handle = kernel32.CreateFileW(str(path), 0, _FILE_SHARE_ALL, None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed for the trusted root")
    try:
        return _final_path_of_handle(handle)
    finally:
        kernel32.CloseHandle(handle)


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
    try:
        root_final = _final_path_of_dir(root)
    except OSError as exc:
        raise UnsafePath("unsafe path: cannot resolve the trusted root") from exc
    fd = os.open(current, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    try:
        opened = os.fstat(fd)
        # A zero file index cannot prove identity; fail closed rather than compare 0 == 0.
        if not opened.st_ino or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafePath("unsafe path: file changed between check and open")
        # Containment is checked on the open handle, not on a name that could be
        # re-pointed after the walk.
        try:
            opened_final = _final_path_of_fd(fd)
        except OSError as exc:
            raise UnsafePath("unsafe path: cannot resolve the opened file") from exc
        if not _inside(root_final, opened_final):
            raise UnsafePath("unsafe path: resolves outside the trusted root")
    except BaseException:
        os.close(fd)
        raise
    return fd
