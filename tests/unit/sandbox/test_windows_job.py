"""Windows Job Object walls: pure struct tests run everywhere, live escape
tests run only on Windows (real guest executions, no mocks)."""

from __future__ import annotations

import ctypes
import sys
import time
import uuid

import pytest

from p2r_sandbox import SandboxLimits, run_python_sandbox, sandbox_capabilities
from p2r_sandbox import _winjob

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object walls")


def test_extended_limits_struct_is_exact():
    info = _winjob.extended_limits(memory_bytes=64 << 20, max_processes=8, cpu_seconds=60)
    basic = info.BasicLimitInformation
    assert basic.LimitFlags == (0x2000 | 0x400 | 0x200 | 0x8 | 0x4)
    assert basic.ActiveProcessLimit == 8
    assert basic.PerJobUserTimeLimit == 60 * 10_000_000
    assert info.JobMemoryLimit == 64 << 20
    # Win32 x64 layout: BASIC=64, IO_COUNTERS=48, +4*SIZE_T
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_winjob.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
        assert ctypes.sizeof(_winjob.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144


def test_job_unavailable_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows has Job Objects")
    with pytest.raises(OSError):
        _winjob.GuestJob(memory_bytes=1 << 20, max_processes=1, cpu_seconds=1)
    assert sandbox_capabilities()["job_object"] is False


@windows_only
def test_job_object_capability_proven():
    assert sandbox_capabilities()["job_object"] is True


@windows_only
def test_job_memory_limit_denies_allocation():
    code = "x = bytearray(300 * 1024 * 1024)\nprint('ALLOCATED')\n"
    result = run_python_sandbox(code, limits=SandboxLimits(max_memory_mb=64))
    assert result.exit_code != 0
    assert b"ALLOCATED" not in result.stdout
    assert b"MemoryError" in result.stderr


@windows_only
def test_job_memory_limit_covers_grandchildren():
    # A child process allocating must be charged to the same job.
    code = (
        "import subprocess, sys\n"
        "r = subprocess.run([sys.executable, '-I', '-c', "
        "\"x = bytearray(300 * 1024 * 1024); print('CHILD-ALLOCATED')\"], "
        "capture_output=True)\n"
        "print('child-exit', r.returncode)\n"
        "print(r.stdout.decode())\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_memory_mb=96))
    assert b"CHILD-ALLOCATED" not in result.stdout, result.stdout


@windows_only
def test_job_process_count_limit():
    code = (
        "import subprocess, sys\n"
        "procs, failed = [], None\n"
        "for i in range(20):\n"
        "    try:\n"
        "        procs.append(subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(5)']))\n"
        "    except OSError as exc:\n"
        "        failed = (i, exc.winerror)\n"
        "        break\n"
        "print('SPAWNED', len(procs), 'FAILED', failed)\n"
        "for p in procs: p.kill()\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_processes=4, max_seconds=30))
    out = result.stdout.decode()
    assert "FAILED None" not in out, out
    # guest itself counts as one process: at most 3 children fit
    spawned = int(out.split("SPAWNED")[1].split()[0])
    assert spawned <= 3, out


def _process_alive(marker: str) -> bool:
    import subprocess

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' "
         f"-and $_.Name -eq 'python.exe' }}).Count"],
        capture_output=True, text=True,
    ).stdout
    return out.strip() not in ("", "0")


@windows_only
def test_background_grandchild_reaped_after_clean_exit():
    marker = f"p2r-bg-{uuid.uuid4().hex}"
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-I', '-c', "
        f"'import time; time.sleep(60)  # {marker}'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=0x00000008)  # DETACHED_PROCESS\n"
        "print('LEADER-DONE')\n"
    )
    started = time.monotonic()
    result = run_python_sandbox(code, limits=SandboxLimits(max_seconds=30))
    assert b"LEADER-DONE" in result.stdout
    assert result.timed_out is False
    assert time.monotonic() - started < 20
    time.sleep(0.5)
    assert not _process_alive(marker)


@windows_only
def test_timeout_kills_whole_tree():
    marker = f"p2r-to-{uuid.uuid4().hex}"
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-I', '-c', "
        f"'import time; time.sleep(60)  # {marker}'])\n"
        "time.sleep(60)\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_seconds=2))
    assert result.timed_out is True
    time.sleep(0.5)
    assert not _process_alive(marker)


@windows_only
def test_breakaway_from_job_does_not_escape_limits():
    # CREATE_BREAKAWAY_FROM_JOB needs JOB_OBJECT_LIMIT_BREAKAWAY_OK, which we
    # never set. Depending on the supervisor's own job nesting, Windows either
    # refuses the launch or starts the child *inside* the job; both are fine,
    # what must never happen is a child that escapes the memory limit.
    child = (
        "try:\n x = bytearray(300 * 1024 * 1024)\n print('CHILD-ALLOCATED')\n"
        "except MemoryError:\n print('CHILD-DENIED')\n"
    )
    code = (
        "import subprocess, sys\n"
        "try:\n"
        f"    r = subprocess.run([sys.executable, '-I', '-c', {child!r}], "
        "creationflags=0x01000000, capture_output=True)\n"
        "    print(r.stdout.decode())\n"
        "except OSError as exc:\n"
        "    print('REFUSED', exc.winerror)\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_memory_mb=64))
    assert b"CHILD-ALLOCATED" not in result.stdout, result.stdout
    assert b"CHILD-DENIED" in result.stdout or b"REFUSED" in result.stdout
