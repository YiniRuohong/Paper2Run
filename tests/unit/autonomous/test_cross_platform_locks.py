"""Exclusive file locks on both host families.

The Windows branch runs on macOS/Linux by forcing ``locks._windows`` and
injecting a fake ``msvcrt`` with LockFile byte-range semantics (a locked byte
refuses every other handle, including handles in the same process).
"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from p2r_orchestration import locks
from p2r_orchestration.research_capabilities import CapabilityRegistry


class FakeMsvcrt:
    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self):
        self.held = {}
        self.calls = []
        self.guard = threading.Lock()

    def locking(self, fd, mode, nbytes):
        position = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((mode, position, nbytes))
        info = os.fstat(fd)
        key = (info.st_dev, info.st_ino, position, nbytes)
        with self.guard:
            owner = self.held.get(key)
            if mode == self.LK_NBLCK:
                if owner is not None:
                    raise OSError(errno.EACCES, "region locked")
                self.held[key] = fd
            elif mode == self.LK_UNLCK:
                if owner != fd:
                    raise OSError(errno.EACCES, "region not locked by this handle")
                del self.held[key]
            else:
                raise AssertionError("helper must use only LK_NBLCK and LK_UNLCK")


@pytest.fixture(params=["posix", "windows"])
def branch(request, monkeypatch):
    fake = None
    if request.param == "windows":
        fake = FakeMsvcrt()
        monkeypatch.setattr(locks, "_windows", lambda: True)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
    else:
        monkeypatch.setattr(locks, "_windows", lambda: False)
    return SimpleNamespace(name=request.param, fake=fake)


def test_nonblocking_contention_and_release_on_exit(branch, tmp_path):
    path = tmp_path / "runtime.lock"
    with locks.exclusive_lock(path, blocking=False):
        with pytest.raises(OSError):
            with locks.exclusive_lock(path, blocking=False):
                pass
    # Released on exit: a new handle acquires immediately.
    with locks.exclusive_lock(path, blocking=False):
        pass
    if branch.fake is not None:
        assert branch.fake.held == {}
        # Always byte 0, length 1, regardless of the file position before locking.
        assert {(position, nbytes) for _, position, nbytes in branch.fake.calls} == {(0, 1)}


def test_held_fd_contention_and_release(branch, tmp_path):
    path = tmp_path / "runtime.lock"
    first = locks.open_lock_file(path)
    locks.acquire(first, blocking=False)
    second = locks.open_lock_file(path)
    try:
        os.write(second, b"moves the file position")
        with pytest.raises(OSError):
            locks.acquire(second, blocking=False)
    finally:
        locks.release(first)
    locks.acquire(second, blocking=False)
    locks.release(second)
    if branch.fake is not None:
        assert branch.fake.held == {}


def test_blocking_acquire_waits_for_release(branch, tmp_path):
    path = tmp_path / "registry.lock"
    holder = locks.open_lock_file(path)
    locks.acquire(holder)
    releaser = threading.Timer(0.25, locks.release, args=(holder,))
    started = time.monotonic()
    releaser.start()
    try:
        with locks.exclusive_lock(path):
            waited = time.monotonic() - started
    finally:
        releaser.join()
    assert waited >= 0.2


def test_windows_unexpected_lock_error_is_not_retried(monkeypatch, tmp_path):
    class Broken(FakeMsvcrt):
        def locking(self, fd, mode, nbytes):
            raise OSError(errno.EBADF, "bad handle")
    monkeypatch.setattr(locks, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", Broken())
    fd = locks.open_lock_file(tmp_path / "x.lock")
    try:
        with pytest.raises(OSError) as error:
            locks.acquire(fd)  # blocking: must raise, never spin on a non-contention error
        assert error.value.errno == errno.EBADF
    finally:
        os.close(fd)


def test_lock_file_symlink_refused_on_both_branches(branch, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = tmp_path / "runtime.lock"
    link.symlink_to(target)
    with pytest.raises(OSError):
        locks.open_lock_file(link)


def test_windows_lock_file_reparse_point_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(locks, "_windows", lambda: True)
    path = tmp_path / "runtime.lock"
    path.write_text("")
    real_lstat = os.lstat

    def lstat(value, *args, **kwargs):
        info = real_lstat(value, *args, **kwargs)
        if os.fspath(value) == str(path):
            return SimpleNamespace(st_mode=info.st_mode, st_ino=info.st_ino, st_dev=info.st_dev,
                                   st_file_attributes=0x400)
        return info
    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(OSError):
        locks.open_lock_file(path)


def test_windows_lock_file_swapped_after_check_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(locks, "_windows", lambda: True)
    path = tmp_path / "runtime.lock"
    path.write_text("")
    real_open = os.open

    def swapping_open(value, flags, *args, **kwargs):
        if os.fspath(value) == str(path):
            replacement = tmp_path / "replacement"
            replacement.write_text("")
            os.replace(replacement, path)
        return real_open(value, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(OSError):
        locks.open_lock_file(path)


def test_registry_lock_uses_the_windows_branch(monkeypatch, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setattr(locks, "_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    registry = CapabilityRegistry(bundled, tmp_path / "imported")
    assert registry.list() == []
    assert fake.calls and fake.held == {}
    assert (tmp_path / "imported" / ".registry.lock").is_file()
