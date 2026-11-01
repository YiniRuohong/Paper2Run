"""Unprivileged Linux confinement: Landlock + seccomp-bpf, then exec.

This file is executed *inside the guest process* as
``python -I -c <this source> <workdir> <target argv...>``. It must therefore
be self-contained (stdlib only, no package imports). It:

1. sets ``PR_SET_NO_NEW_PRIVS`` (required by both Landlock and seccomp);
2. builds a Landlock ruleset handling every filesystem right the running
   kernel knows: the workdir gets full access, ``/dev/null`` read/write,
   system and interpreter trees read+execute only - everything else
   (``$HOME``, ``/root``, ``/tmp``, ``/var``, ``/run``, ...) is invisible;
   on ABI>=4 TCP bind/connect are handled with no allow rule (denied); on
   ABI>=6 signals and abstract unix sockets are scoped to the guest domain;
   Landlock also denies ptrace-style access (``/proc/<pid>/environ``,
   ``/proc/<pid>/mem``) to processes outside the domain;
3. installs a seccomp-bpf filter that refuses ``socket()`` and the
   ``io_uring`` syscalls with ``EPERM`` (UDP/raw/netlink sockets are not
   covered by Landlock's TCP rules) and kills foreign syscall ABIs (i386
   compat, x32);
4. ``execv``\\ s the target. Restrictions are inherited and irrevocable.

Any failure before the exec prints :data:`FAIL_MARKER` to stderr and exits
with :data:`FAIL_EXIT_CODE` - the target never runs unconfined.

Pure helpers (rights per ABI, BPF program) are importable for unit tests.
"""

from __future__ import annotations

import os
import struct
import sys

FAIL_EXIT_CODE = 121
FAIL_MARKER = "[p2r_sandbox] landlock launcher failed:"

# --- Landlock -------------------------------------------------------------
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5

NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1  # ABI 6

FS_READ_EXEC = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
#: Rights that are valid on a non-directory rule target.
FS_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

#: System trees the interpreter and common native libraries need (read+exec).
SYSTEM_READ_PATHS = (
    "/usr", "/lib", "/lib32", "/lib64", "/libx32", "/bin", "/sbin",
    "/etc", "/opt", "/proc", "/sys", "/dev",
)


def handled_fs(abi: int) -> int:
    rights = (1 << 13) - 1  # ABI 1: EXECUTE..MAKE_SYM
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


def handled_net(abi: int) -> int:
    return NET_BIND_TCP | NET_CONNECT_TCP if abi >= 4 else 0


def scoped(abi: int) -> int:
    return SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0


def ruleset_attr(abi: int) -> bytes:
    """``struct landlock_ruleset_attr`` sized for the kernel ABI."""
    if abi >= 6:
        return struct.pack("=QQQ", handled_fs(abi), handled_net(abi), scoped(abi))
    if abi >= 4:
        return struct.pack("=QQ", handled_fs(abi), handled_net(abi))
    return struct.pack("=Q", handled_fs(abi))


def path_beneath_attr(allowed: int, fd: int) -> bytes:
    """``struct landlock_path_beneath_attr`` (packed: u64 + s32)."""
    return struct.pack("=Qi", allowed, fd)


# --- seccomp-bpf ----------------------------------------------------------
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
X32_SYSCALL_BIT = 0x40000000

#: machine -> (audit arch, syscalls refused with EPERM)
ARCH_TABLE = {
    # socket, io_uring_setup, io_uring_enter, io_uring_register
    "x86_64": (AUDIT_ARCH_X86_64, (41, 425, 426, 427)),
    "aarch64": (AUDIT_ARCH_AARCH64, (198, 425, 426, 427)),
}

BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_RET_K = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1


def bpf_program(machine: str) -> list[tuple[int, int, int, int]]:
    """Seccomp filter as ``(code, jt, jf, k)`` tuples (pure)."""
    arch, denied = ARCH_TABLE[machine]
    prog = [
        (BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        (BPF_JEQ_K, 1, 0, arch),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if machine == "x86_64":
        prog.append((BPF_JGE_K, 0, 1, X32_SYSCALL_BIT))
        prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | EPERM))
    n = len(denied)
    for i, nr in enumerate(denied):
        # match -> jump over the remaining compares and the ALLOW to ERRNO
        prog.append((BPF_JEQ_K, n - i, 0, nr))
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | EPERM))
    return prog


def pack_program(prog) -> bytes:
    return b"".join(struct.pack("=HBBI", c, jt, jf, k) for c, jt, jf, k in prog)


def read_paths(workdir: str, target: str) -> list[str]:
    """Interpreter + system trees granted read/exec (never $HOME as such)."""
    paths = list(SYSTEM_READ_PATHS)
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix):
        paths.append(p)
    for exe in (sys.executable, target):
        if exe:
            paths.append(os.path.dirname(os.path.abspath(exe)))
            paths.append(os.path.dirname(os.path.realpath(exe)))
    seen, out = set(), []
    for p in paths:
        if p and p not in seen and p != workdir:
            seen.add(p)
            out.append(p)
    return out


# --- live part (Linux only) -------------------------------------------------


def _confine(workdir: str, target: str) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    L = ctypes.c_long
    UL = ctypes.c_ulong

    def check(rc, what):
        if rc < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"{what}: {os.strerror(err)}")
        return rc

    machine = os.uname().machine
    if machine not in ARCH_TABLE:
        raise OSError(0, f"unsupported architecture {machine}")
    abi = check(libc.syscall(L(SYS_LANDLOCK_CREATE_RULESET), None, UL(0),
                             UL(LANDLOCK_CREATE_RULESET_VERSION)), "landlock abi")
    if abi < 1:
        raise OSError(0, "landlock unavailable")
    check(libc.prctl(PR_SET_NO_NEW_PRIVS, UL(1), UL(0), UL(0), UL(0)), "no_new_privs")

    attr = ruleset_attr(abi)
    abuf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = check(libc.syscall(L(SYS_LANDLOCK_CREATE_RULESET), abuf,
                                 UL(len(attr)), UL(0)), "create_ruleset")
    fs = handled_fs(abi)
    rules = [(workdir, fs, True), ("/dev/null", FS_READ_FILE | FS_WRITE_FILE
                                   | FS_TRUNCATE | FS_IOCTL_DEV, False)]
    rules += [(p, FS_READ_EXEC, False) for p in read_paths(workdir, target)]
    for path, rights, required in rules:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except FileNotFoundError:
            if required:
                raise
            continue
        try:
            if not os.path.isdir(path):
                rights &= FS_FILE_RIGHTS
            rights &= fs
            rb = path_beneath_attr(rights, fd)
            rbuf = ctypes.create_string_buffer(rb, len(rb))
            check(libc.syscall(L(SYS_LANDLOCK_ADD_RULE), L(ruleset),
                               L(LANDLOCK_RULE_PATH_BENEATH), rbuf, UL(0)),
                  f"add_rule {path}")
        finally:
            os.close(fd)
    check(libc.syscall(L(SYS_LANDLOCK_RESTRICT_SELF), L(ruleset), UL(0)), "restrict_self")
    os.close(ruleset)

    filt = pack_program(bpf_program(machine))
    count = len(filt) // 8

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fbuf = ctypes.create_string_buffer(filt, len(filt))
    fprog = SockFprog(count, ctypes.cast(fbuf, ctypes.c_void_p))
    check(libc.prctl(PR_SET_SECCOMP, UL(SECCOMP_MODE_FILTER),
                     ctypes.byref(fprog), UL(0), UL(0)), "seccomp")


def main(argv: list[str]) -> None:
    try:
        if len(argv) < 3:
            raise OSError(0, "usage: launcher <workdir> <target argv...>")
        workdir, target = argv[1], argv[2:]
        _confine(workdir, target[0])
    except BaseException as exc:  # fail closed: never exec unconfined
        sys.stderr.write(f"{FAIL_MARKER} {exc}\n")
        sys.stderr.flush()
        os._exit(FAIL_EXIT_CODE)
    os.execv(target[0], target)


if __name__ == "__main__":
    main(sys.argv)
