"""Landlock + seccomp launcher: pure rights/BPF tests (every OS) and live
walls (Linux hosts where the probe enabled the backend)."""

from __future__ import annotations

import os
import struct
import sys

import pytest

from p2r_sandbox import _landlock_launcher as ll
from p2r_sandbox import run_python_sandbox, sandbox_capabilities
from p2r_sandbox.sandbox import _landlock_argv, _wrapped_argv


# --- rights per ABI -------------------------------------------------------


def test_handled_rights_grow_with_abi():
    assert ll.handled_fs(1) == (1 << 13) - 1
    assert ll.handled_fs(2) == ll.handled_fs(1) | ll.FS_REFER
    assert ll.handled_fs(3) == ll.handled_fs(2) | ll.FS_TRUNCATE
    assert ll.handled_fs(4) == ll.handled_fs(3)
    assert ll.handled_fs(5) == ll.handled_fs(4) | ll.FS_IOCTL_DEV
    assert ll.handled_fs(10) == ll.handled_fs(5)  # unknown future bits never sent
    assert ll.handled_net(3) == 0
    assert ll.handled_net(4) == ll.NET_BIND_TCP | ll.NET_CONNECT_TCP
    assert ll.scoped(5) == 0
    assert ll.scoped(6) == ll.SCOPE_ABSTRACT_UNIX_SOCKET | ll.SCOPE_SIGNAL


@pytest.mark.parametrize("abi,size", [(1, 8), (3, 8), (4, 16), (5, 16), (6, 24), (9, 24)])
def test_ruleset_attr_size_matches_abi(abi, size):
    attr = ll.ruleset_attr(abi)
    assert len(attr) == size
    assert struct.unpack_from("=Q", attr)[0] == ll.handled_fs(abi)


def test_path_beneath_attr_is_packed():
    blob = ll.path_beneath_attr(ll.FS_READ_EXEC, 7)
    assert len(blob) == 12
    assert struct.unpack("=Qi", blob) == (ll.FS_READ_EXEC, 7)


def test_read_paths_never_grant_home_or_tmp():
    home = os.path.expanduser("~")
    paths = ll.read_paths("/w", sys.executable)
    assert "/usr" in paths and "/proc" in paths
    assert "/tmp" not in paths and "/var" not in paths and "/" not in paths
    assert home not in paths or home in (sys.prefix, sys.base_prefix)
    assert "/w" not in paths


# --- seccomp-bpf program, executed by a tiny interpreter ------------------


def _run_bpf(prog, arch: int, nr: int) -> int:
    data = {0: nr, 4: arch}
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == ll.BPF_LD_W_ABS:
            acc = data[k]
            pc += 1
        elif code == ll.BPF_JEQ_K:
            pc += 1 + (jt if acc == k else jf)
        elif code == ll.BPF_JGE_K:
            pc += 1 + (jt if acc >= k else jf)
        elif code == ll.BPF_RET_K:
            return k
        else:  # pragma: no cover
            raise AssertionError(code)


ERRNO_EPERM = ll.SECCOMP_RET_ERRNO | ll.EPERM


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_bpf_denies_socket_and_io_uring_allows_rest(machine):
    arch, denied = ll.ARCH_TABLE[machine]
    prog = ll.bpf_program(machine)
    for nr in denied:
        assert _run_bpf(prog, arch, nr) == ERRNO_EPERM, nr
    socketpair = 53 if machine == "x86_64" else 199
    for nr in (0, 1, 2, 59, 60, 221, socketpair, 424, 428):
        assert _run_bpf(prog, arch, nr) == ll.SECCOMP_RET_ALLOW, nr
    # a foreign syscall ABI (e.g. i386 compat) is killed outright
    assert _run_bpf(prog, 0x40000003, 41) == ll.SECCOMP_RET_KILL_PROCESS


def test_bpf_refuses_x32_syscalls():
    prog = ll.bpf_program("x86_64")
    assert _run_bpf(prog, ll.AUDIT_ARCH_X86_64, ll.X32_SYSCALL_BIT | 41) == ERRNO_EPERM
    assert _run_bpf(prog, ll.AUDIT_ARCH_X86_64, ll.X32_SYSCALL_BIT | 0) == ERRNO_EPERM


def test_bpf_packing_is_8_bytes_per_insn():
    prog = ll.bpf_program("aarch64")
    assert len(ll.pack_program(prog)) == 8 * len(prog)


# --- argv -----------------------------------------------------------------


def test_landlock_argv_runs_launcher_in_guest():
    argv = _landlock_argv(["/py/bin/python3", "-I", "script.py"], "/w/x")
    assert argv[:3] == ["/py/bin/python3", "-I", "-c"]
    assert "def _confine" in argv[3]
    assert argv[4:] == ["/w/x", "/py/bin/python3", "-I", "script.py"]
    full = _wrapped_argv("landlock", "/py/bin/python3", "/w/x")
    assert full[-3:] == [os.path.abspath("/py/bin/python3"), "-I", "script.py"]


def test_launcher_fails_closed_without_target():
    # main() must exit with the marker instead of running anything
    import subprocess

    with open(ll.__file__, encoding="utf-8") as fh:
        source = fh.read()
    proc = subprocess.run([sys.executable, "-I", "-c", source, "/nowhere"], capture_output=True)
    assert proc.returncode == ll.FAIL_EXIT_CODE
    assert ll.FAIL_MARKER.encode() in proc.stderr


# --- live walls ------------------------------------------------------------

landlock_live = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not sandbox_capabilities()["landlock"],
    reason="Landlock backend not available on this host",
)


@landlock_live
def test_landlock_denies_udp_and_unix_sockets():
    # Landlock's net rules only cover TCP; seccomp must refuse every socket()
    code = (
        "import socket\n"
        "for fam, typ in ((socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_UNIX, socket.SOCK_STREAM)):\n"
        "    try:\n"
        "        socket.socket(fam, typ); print('SOCKET-OK')\n"
        "    except PermissionError:\n"
        "        print('SOCKET-DENIED')\n"
    )
    from p2r_sandbox.sandbox import _select_backend

    if _select_backend()[0] != "landlock":
        pytest.skip("another backend is preferred on this host")
    result = run_python_sandbox(code)
    assert result.stdout.count(b"SOCKET-DENIED") == 2, result.stdout


@landlock_live
def test_landlock_denies_home_and_parent_environ():
    from p2r_sandbox.sandbox import _select_backend

    if _select_backend()[0] != "landlock":
        pytest.skip("another backend is preferred on this host")
    code = (
        "import os\n"
        "for p in [os.path.expanduser('~'), '/proc/%d/environ' % os.getppid(), '/proc/1/environ']:\n"
        "    try:\n"
        "        (os.listdir(p) if os.path.isdir(p) else open(p, 'rb').read(1)); print('READ', p)\n"
        "    except OSError as e:\n"
        "        print('DENIED', p, e.errno)\n"
        "import json, sqlite3, ssl; print('STDLIB-OK')\n"
    )
    result = run_python_sandbox(code)
    out = result.stdout.decode()
    assert "READ" not in out, out
    assert "STDLIB-OK" in out
