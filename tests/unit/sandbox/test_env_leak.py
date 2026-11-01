"""Cross-process environment leak: can a guest read the *supervisor's*
start-up environment (where API keys usually live)?

A fresh supervisor process is started with a random canary in its
environment and runs one guest. The guest tries the platform's strongest
same-user technique:

* Linux: every ``/proc/<pid>/environ``;
* macOS: ``sysctl(KERN_PROCARGS2)`` of the parent (argv + envp);
* Windows: ``OpenProcess`` + the PEB's environment block via
  ``ReadProcessMemory``.

Under ``backend="none"`` the read must succeed - this proves the detector
works. Isolating backends (bwrap/unshare/landlock/appcontainer) must block
it. seatbelt cannot (documented limitation, see docs/components/sandbox.md
section 7): the test is an expected failure there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SANDBOX_PKG = str(Path(__file__).resolve().parents[3] / "packages" / "sandbox")

GUEST_LINUX = r"""
import os
hits = 0
for pid in os.listdir('/proc'):
    if pid.isdigit():
        try:
            with open('/proc/%s/environ' % pid, 'rb') as fh:
                hits += CANARY in fh.read()
        except OSError:
            pass
print('LEAK' if hits else 'NO-LEAK')
"""

GUEST_DARWIN = r"""
import ctypes, os
libc = ctypes.CDLL(None)
mib = (ctypes.c_int * 3)(1, 49, os.getppid())  # CTL_KERN, KERN_PROCARGS2
size = ctypes.c_size_t(1 << 20)
buf = ctypes.create_string_buffer(size.value)
ok = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) == 0
print('LEAK' if ok and CANARY in buf.raw[:size.value] else 'NO-LEAK')
"""

GUEST_WINDOWS = r"""
import ctypes, os
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
nt = ctypes.WinDLL('ntdll')
k32.OpenProcess.restype = ctypes.c_void_p
k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
nt.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                         ctypes.c_ulong, ctypes.c_void_p]
def leaked():
    h = k32.OpenProcess(0x0410, False, SUPERVISOR_PID)
    if not h:
        return False
    pbi = (ctypes.c_void_p * 6)()
    if nt.NtQueryInformationProcess(h, 0, pbi, ctypes.sizeof(pbi), None) != 0:
        return False
    def read(addr, n):
        buf = ctypes.create_string_buffer(n); got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]
    peb = pbi[1]
    params = read(peb + 0x20, 8)
    if not params:
        return False
    params = int.from_bytes(params, 'little')
    env_ptr = int.from_bytes(read(params + 0x80, 8) or b'\0' * 8, 'little')
    env_size = int.from_bytes(read(params + 0x3F0, 8) or b'\0' * 8, 'little')
    block = read(env_ptr, min(env_size or 65536, 1 << 20)) or b''
    return CANARY.decode().encode('utf-16-le') in block
print('LEAK' if leaked() else 'NO-LEAK')
"""

SUPERVISOR = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from p2r_sandbox import run_python_sandbox
guest = sys.argv[2].replace('SUPERVISOR_PID', str(os.getpid()))
result = run_python_sandbox(guest)
print(json.dumps({"backend": result.backend, "stdout": result.stdout.decode('utf-8', 'replace'),
                  "stderr": result.stderr.decode('utf-8', 'replace')[-500:]}))
"""


def _guest_source(canary: str) -> str:
    if sys.platform.startswith("linux"):
        template = GUEST_LINUX
    elif sys.platform == "darwin":
        template = GUEST_DARWIN
    elif sys.platform == "win32":
        template = GUEST_WINDOWS
    else:  # pragma: no cover
        pytest.skip("unsupported platform")
    return template.replace("CANARY", repr(canary.encode()))


def _run_supervisor(force_backend: str | None = None) -> dict:
    canary = f"p2r-canary-{uuid.uuid4().hex}"
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LOCALAPPDATA",
                                            "TEMP", "TMP", "HOME", "USERPROFILE")
           if key in os.environ}
    env["P2R_LEAK_CANARY"] = canary
    supervisor = SUPERVISOR
    if force_backend is not None:
        supervisor = supervisor.replace(
            "from p2r_sandbox import run_python_sandbox",
            "from p2r_sandbox import run_python_sandbox\n"
            "from p2r_sandbox import sandbox as _m\n"
            f"_m._select_backend = lambda: ({force_backend!r}, False)",
        )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", supervisor, SANDBOX_PKG, _guest_source(canary)],
        env=env, capture_output=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])


def test_detector_sees_leak_without_isolation():
    """Sanity: with no isolation the technique must work, else the
    isolated-backend assertion below would prove nothing."""
    data = _run_supervisor(force_backend="none")
    assert data["backend"] == "none"
    assert "LEAK" in data["stdout"] and "NO-LEAK" not in data["stdout"], data


def test_supervisor_environment_not_readable_from_guest():
    data = _run_supervisor()
    backend = data["backend"]
    if backend == "none":
        pytest.skip("no isolating backend on this host (fail-closed callers refuse to run)")
    if backend == "seatbelt" and "NO-LEAK" not in data["stdout"]:
        pytest.xfail(
            "seatbelt cannot deny sysctl(KERN_PROCARGS2) for same-uid processes; "
            "documented limitation - keep secrets out of the supervisor's start-up env"
        )
    assert "NO-LEAK" in data["stdout"], data
