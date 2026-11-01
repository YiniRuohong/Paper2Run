"""Escape regression tests: real guest executions, no mocks.

Each test asserts a wall the sandbox is supposed to enforce on this host,
falling back to honest-consistency checks where the platform cannot provide
the wall (never silently assuming isolation).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from p2r_sandbox import SandboxLimits, SandboxResult, run_python_sandbox, sandbox_capabilities


DARWIN_PLATFORM_ENV_KEYS = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
WHITELIST_ENV_KEYS = {"PATH", "TMPDIR"} | (
    DARWIN_PLATFORM_ENV_KEYS if sys.platform == "darwin" else set()
)


# ---------------------------------------------------------------------------
# interpreter / stdlib availability under the isolation stack
# ---------------------------------------------------------------------------


def test_stdlib_importable_under_isolation():
    # stdlib must stay readable even though sensitive paths are denied:
    # ssl/sqlite3 drag in extension modules from the installation tree.
    code = (
        "import hashlib, json, pathlib, sqlite3, ssl, urllib.parse\n"
        "print('STDLIB-OK')\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code == 0
    assert b"STDLIB-OK" in result.stdout


# ---------------------------------------------------------------------------
# sensitive-path read denial (macOS seatbelt)
# ---------------------------------------------------------------------------


def test_sensitive_read_denied_under_seatbelt():
    caps = sandbox_capabilities()
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        pytest.skip("seatbelt not applicable on this host")
    # On a seatbelt host the capability must be proven, never assumed.
    assert caps["seatbelt"] is True
    assert caps["sensitive_read_denied"] is True
    keychains = os.path.expanduser("~/Library/Keychains")
    code = (
        "import os\n"
        f"os.listdir({keychains!r})\n"
        "print('SENSITIVE-READ-OK')\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code != 0
    assert b"SENSITIVE-READ-OK" not in result.stdout
    # seatbelt denials surface as "Operation not permitted" (EPERM)
    assert b"Operation not permitted" in result.stderr


def test_sensitive_read_of_minimax_denied_when_present():
    caps = sandbox_capabilities()
    if not caps["sensitive_read_denied"]:
        pytest.skip("sensitive-read denial not verified on this host")
    minimax = os.path.expanduser("~/.minimax")
    if not os.path.isdir(minimax):
        pytest.skip("~/.minimax does not exist on this host")
    code = (
        "import os\n"
        f"os.listdir({minimax!r})\n"
        "print('MINIMAX-READ-OK')\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code != 0
    assert b"MINIMAX-READ-OK" not in result.stdout
    assert b"Operation not permitted" in result.stderr


# ---------------------------------------------------------------------------
# network denial
# ---------------------------------------------------------------------------


def test_network_connect_denied():
    caps = sandbox_capabilities()
    code = (
        "import socket\n"
        "socket.create_connection(('127.0.0.1', 1), timeout=2)\n"
        "print('CONNECTED-BAD')\n"
    )
    result = run_python_sandbox(code)
    assert result.network_isolated is caps["seatbelt"]
    assert result.exit_code != 0
    assert b"CONNECTED-BAD" not in result.stdout


# ---------------------------------------------------------------------------
# outside-write denial
# ---------------------------------------------------------------------------


PROBE_OUTSIDE = "/tmp/p2r_sbx_escape_probe"


def test_write_outside_workdir_denied():
    caps = sandbox_capabilities()
    code = (
        f"open({PROBE_OUTSIDE!r}, 'w').write('leaked')\n"
        "print('WROTE-OUTSIDE-BAD')\n"
    )
    try:
        result = run_python_sandbox(code)
        if caps["seatbelt"]:
            assert result.network_isolated is True
            assert result.exit_code != 0
            assert b"Operation not permitted" in result.stderr
            assert not os.path.exists(PROBE_OUTSIDE)
        else:
            # Degraded mode must not claim isolation it does not have.
            assert result.network_isolated is False
    finally:
        if os.path.exists(PROBE_OUTSIDE):
            os.unlink(PROBE_OUTSIDE)


# ---------------------------------------------------------------------------
# collection escapes
# ---------------------------------------------------------------------------


def test_symlink_escape_collect_refused():
    # /etc/hosts exists on macOS and Linux and is readable - the point is
    # that a symlink inside the workdir pointing at it must be refused.
    code = (
        "import os\n"
        "os.symlink('/etc/hosts', 'predictions.json')\n"
        "print('symlinked')\n"
    )
    result = run_python_sandbox(code, collect_files=("predictions.json",))
    assert result.exit_code == 0
    assert "predictions.json" not in result.files
    assert result.truncated is True
    assert b"escapes workdir" in result.stderr


def test_symlink_escape_via_glob_collect_refused():
    code = (
        "import os\n"
        "os.symlink('/etc/hosts', 'evil.json')\n"
        "open('good.json', 'w').write('{}')\n"
        "print('done')\n"
    )
    result = run_python_sandbox(code, collect_files=("*.json",))
    assert result.exit_code == 0
    assert result.files == {"good.json": b"{}"}
    assert result.truncated is True
    assert b"escapes workdir" in result.stderr


def test_collect_glob_matches_top_level_only():
    code = (
        "import os\n"
        "os.makedirs('sub')\n"
        "open('top.json', 'w').write('{}')\n"
        "open('sub/nested.json', 'w').write('{}')\n"
    )
    result = run_python_sandbox(code, collect_files=("*.json",))
    assert result.exit_code == 0
    assert set(result.files) == {"top.json"}


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def test_guest_env_is_whitelist_no_host_leak(monkeypatch):
    monkeypatch.setenv("P2R_ESCAPE_SECRET", "escape-secret-value")
    code = (
        "import json, os\n"
        "print(json.dumps(sorted(os.environ.keys())))\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code == 0
    keys = set(json.loads(result.stdout.decode().strip()))
    assert keys <= WHITELIST_ENV_KEYS
    assert b"escape-secret-value" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# resource attacks
# ---------------------------------------------------------------------------


def test_stdout_flood_truncated():
    code = (
        "import sys\n"
        "chunk = b'x' * 65536\n"
        "for _ in range(32):\n"  # 2 MiB total
        "    sys.stdout.buffer.write(chunk)\n"
        "sys.stdout.buffer.flush()\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_output_bytes=8192))
    assert result.exit_code == 0
    assert len(result.stdout) == 8192
    assert result.truncated is True


def _nproc_allowing_forks() -> int:
    """max_processes high enough that guest forks actually succeed here.

    RLIMIT_NPROC counts *all* processes of the real uid (enforced on this
    macOS host: a child with (8, 8) gets EAGAIN on fork), so the value must
    exceed the host's current uid process count while staying within the
    hard limit.
    """
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    except (ImportError, AttributeError, OSError):
        return 256
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-U", str(os.getuid())],
            capture_output=True, text=True, timeout=10,
        )
        current = len([line for line in out.stdout.splitlines() if line.strip()])
    except (OSError, subprocess.SubprocessError):
        current = 0
    wanted = current + 64
    if hard == -1:  # RLIM_INFINITY
        return wanted
    return max(64, min(wanted, hard))


FORKBOMB_MARKER_PREFIX = "p2r-forkbomb-marker-"

FORKBOMB_CODE = (
    "import subprocess, sys\n"
    "args = [sys.executable, '-I', '-c', "
    "'import time; time.sleep(120)', " + repr(FORKBOMB_MARKER_PREFIX) + "]\n"
    "while True:\n"
    "    try:\n"
    "        subprocess.Popen(args, stdout=subprocess.DEVNULL, "
    "stderr=subprocess.DEVNULL)\n"
    "    except OSError:\n"
    "        pass\n"
)


def _marker_survivors() -> list[str]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=15
    )
    return [
        line.strip()
        for line in out.stdout.splitlines()
        if FORKBOMB_MARKER_PREFIX in line
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="ps/killpg unavailable")
def test_forkbomb_stopped_no_residual_processes():
    marker = FORKBOMB_MARKER_PREFIX + uuid.uuid4().hex[:8]
    code = FORKBOMB_CODE.replace(FORKBOMB_MARKER_PREFIX, marker)
    result = run_python_sandbox(
        code,
        limits=SandboxLimits(max_seconds=3, max_processes=_nproc_allowing_forks()),
    )
    # Either NPROC stopped the spawning (guest crash) or the wall clock
    # killed the group - the run must not end cleanly.
    assert result.exit_code != 0 or result.timed_out
    for _ in range(30):  # up to ~3s for SIGKILL delivery to be observable
        # match this run's marker only, so leftovers of other runs cannot mask/fake a result
        survivors = [line for line in _marker_survivors() if marker in line]
        if not survivors:
            break
        time.sleep(0.1)
    assert survivors == []
    assert b"wall-clock limit exceeded" in result.stderr or result.exit_code != 0


@pytest.mark.skipif(sys.platform == "win32", reason="ps/killpg unavailable")
def test_descendant_memory_bomb_killed_via_tree_monitoring():
    # The leader stays tiny; a *grandchild* hoards memory. Only a tree-wide
    # RSS monitor can catch this - leader-only monitoring would let it run
    # to the wall clock and fail this test's memory-note assertion.
    marker = uuid.uuid4().hex[:8]
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([\n"
        "    sys.executable, '-I', '-c',\n"
        "    \"x = bytearray(400 * 1024 * 1024)\\nimport time\\ntime.sleep(60)\",\n"
        f"    'p2r-membomb-{marker}',\n"
        "])\n"
        "print('spawned', child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    result = run_python_sandbox(
        code,
        limits=SandboxLimits(
            max_seconds=10, max_memory_mb=64, max_processes=_nproc_allowing_forks()
        ),
    )
    assert result.exit_code != 0
    assert result.timed_out is False, (
        "guest survived to wall clock: tree RSS monitoring did not see the "
        "grandchild's memory"
    )
    assert b"memory limit exceeded" in result.stderr
    for _ in range(30):
        out = subprocess.run(
            ["ps", "-eo", "args="], capture_output=True, text=True, timeout=15
        )
        if f"p2r-membomb-{marker}" not in out.stdout:
            break
        time.sleep(0.1)
    assert f"p2r-membomb-{marker}" not in out.stdout


# ---------------------------------------------------------------------------
# result plumbing
# ---------------------------------------------------------------------------


def test_result_backend_field_defaults_to_none_for_old_callsites():
    # Every pre-existing construction pattern must keep working.
    result = SandboxResult(
        exit_code=0, stdout=b"", stderr=b"", timed_out=False,
        truncated=False, network_isolated=False, files={}, duration_ms=1,
    )
    assert result.backend == "none"


def test_run_reports_actual_backend():
    from p2r_sandbox.sandbox import _select_backend

    expected_backend, expected_ni = _select_backend()
    result = run_python_sandbox("print('x')")
    assert result.backend == expected_backend
    assert result.network_isolated is expected_ni
    if sys.platform == "darwin":
        caps = sandbox_capabilities()
        assert result.backend == ("seatbelt" if caps["seatbelt"] else "none")


def test_unshare_backend_notes_write_gap_honestly(monkeypatch):
    from p2r_sandbox import sandbox as mod

    # Simulate a Linux host that selected the unshare backend; the argv is
    # substituted so the note path can be exercised on any host (no unshare
    # binary exists on macOS - we must not pretend to launch it).
    monkeypatch.setattr(mod, "_select_backend", lambda: ("unshare", True))
    monkeypatch.setattr(
        mod, "_wrapped_argv", lambda backend, python, workdir: [python, "-I", "script.py"]
    )
    result = run_python_sandbox("print('x')")
    assert result.backend == "unshare"
    assert result.network_isolated is True
    assert b"writes outside the workdir are NOT restricted" in result.stderr


def test_none_backend_reports_no_isolation(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod, "_select_backend", lambda: ("none", False))
    result = run_python_sandbox("print('x')")
    assert result.backend == "none"
    assert result.network_isolated is False


@pytest.mark.skipif(sys.platform == "win32", reason="ps/killpg unavailable")
def test_background_child_of_cleanly_exiting_guest_is_reaped():
    """A guest that spawns a background child and exits 0 must not leave it running."""
    marker = FORKBOMB_MARKER_PREFIX + "bg-" + uuid.uuid4().hex[:8]
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)', "
        + repr(marker) + "], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('spawned')\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_seconds=10, max_processes=_nproc_allowing_forks()))
    assert result.exit_code == 0 and not result.timed_out
    for _ in range(30):
        survivors = [line for line in _marker_survivors() if marker in line]
        if not survivors:
            break
        time.sleep(0.1)
    assert survivors == []
