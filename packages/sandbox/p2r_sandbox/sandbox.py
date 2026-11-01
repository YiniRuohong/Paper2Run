"""Isolated execution of untrusted Python scripts on the Paper2Run host.

Security model
--------------
Every call to :func:`run_python_sandbox` runs the guest script with all of
the isolation layers below. Layers that the host platform cannot provide are
reported honestly (never silently dropped):

1. **Separate process** launched from ``python_exe`` with ``-I`` (isolated
   mode: no user site-packages, no ``PYTHON*`` env influence).
2. **Own session / process group** (``start_new_session=True``) so the whole
   guest process tree can be killed with one ``os.killpg`` call.
3. **Whitelist-constructed environment** - the guest inherits *nothing* from
   the host environment. Only ``PATH`` (python dir + ``/usr/bin:/bin`` +
   ``~/usr/bin``) and ``TMPDIR`` (the sandbox workdir) are constructed by us,
   so API keys and other host secrets cannot leak into the guest. (macOS
   CoreFoundation additionally injects ``LC_CTYPE`` /
   ``__CF_USER_TEXT_ENCODING`` into any process at startup; those are
   platform constants, not host-inherited values.)
4. **Throwaway working directory** (``tempfile.mkdtemp``), deleted in a
   ``finally`` block. The guest script is written to ``workdir/script.py``
   and executed there.
5. **Resource limits** via ``preexec_fn`` rlimits: ``RLIMIT_CPU``,
   ``RLIMIT_NPROC``, ``RLIMIT_FSIZE`` (4 MB) and - where the OS accepts it -
   ``RLIMIT_AS``. On this macOS host ``setrlimit(RLIMIT_AS, ...)`` fails
   with ``EINVAL`` for any finite value (verified empirically), so memory is
   instead enforced by an RSS monitor that sums resident memory over the
   guest's whole process tree (``ps -o pid=,rss= -g <pgid>`` on Darwin;
   ``/proc`` scanning on Linux) and kills the entire process group with
   ``SIGKILL`` once the tree total exceeds ``max_memory_mb``. This
   substitution is recorded in ``SandboxResult.stderr`` - never silent.
6. **Wall-clock timeout**: output is pumped incrementally with a deadline;
   on expiry the whole process group gets ``SIGKILL`` and the result is
   marked ``timed_out=True``.
7. **Output caps**: stdout/stderr are each truncated to
   ``max_output_bytes`` while being read (so a guest flooding stdout cannot
   exhaust host memory), with ``truncated=True`` marking.
8. **macOS seatbelt** (``/usr/bin/sandbox-exec``) when available and
   *verified effective*: a profile that denies all ``network*`` operations,
   all ``file-write*`` operations except inside the (real-path) sandbox
   workdir plus ``/dev/null``, and ``file-read*`` for well-known sensitive
   host locations (``~/.ssh``, ``~/.aws``, ``~/.config``, ``~/.gnupg``,
   ``~/.minimax``, ``~/Library/Keychains``, ``~/Library/Application Support``,
   ``/etc/ssh``, ``/private/etc/ssh`` and ``~/.env*``). Effectiveness is
   proven by a one-time probe (a child under the profile must get
   ``EPERM``/``EACCES`` when connecting to ``127.0.0.1:1``, when writing
   outside the workdir and when listing an existing sensitive directory) and
   the probe child also imports stdlib modules, proving the interpreter and
   standard library stay readable. If ``sandbox-exec`` is missing or the
   probe fails, the run still happens but ``network_isolated=False`` and
   ``sandbox_capabilities()["seatbelt"]`` is ``False`` - we never pretend to
   isolate when we cannot.
9. **Linux backends, probe-gated**: when seatbelt is unavailable the package
   tries, in order, ``bwrap`` (bubblewrap: fresh network namespace, whole-FS
   read-only bind, writable workdir bind, private ``/tmp``) and then
   ``unshare -rn`` (network namespace + uid mapping only - *no* write
   isolation, reported honestly in ``stderr``). Each backend is enabled only
   after a live probe child proves the claimed denial; if neither backend
   works the run degrades to ``backend="none"`` / ``network_isolated=False``.

No shell is ever involved: ``Popen`` receives literal argument lists only.
The chosen backend is reported as ``SandboxResult.backend``:
``"seatbelt"`` / ``"bwrap"`` / ``"unshare"`` / ``"none"``.
"""

from __future__ import annotations

import ctypes
import fnmatch
import json
import os
import re
import select
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no resource module
    resource = None  # type: ignore[assignment]
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .errors import SandboxUnavailable

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
#: Hard cap for any single collected output file (task contract).
MAX_COLLECT_FILE_BYTES = 256 * 1024
#: Guest processes may not create files larger than this (RLIMIT_FSIZE).
RLIMIT_FSIZE_BYTES = 4 * 1024 * 1024
#: Pump granularity: bounds both RSS-monitor latency and idle wakeups.
_POLL_INTERVAL_SECONDS = 0.05
#: Leader RSS is a cheap syscall; sample it on this cadence so a fast
#: allocation burst cannot slip between output-driven wakeups.
_LEADER_RSS_INTERVAL_SECONDS = 0.015
#: Whole-process-tree RSS needs a process enumeration (ps subprocess on
#: Darwin, /proc scan on Linux) that is 1-2 orders of magnitude costlier;
#: sweeping on this slower cadence bounds descendant-hoarding detection
#: without spawning a process enumerator every 15 ms.
_TREE_RSS_INTERVAL_SECONDS = 0.2
#: macOS injects these into every process at startup (CoreFoundation); they
#: are not inherited from the host environment and carry no secrets.
_PLATFORM_INJECTED_ENV_KEYS = frozenset({"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})

#: Sensitive host locations the seatbelt profile denies for *reads*. Paths
#: starting with ``~`` are expanded against the real home directory at
#: profile-build time. The kernel matches resolved paths, so symlink aliases
#: of the same location are covered by listing both spellings (e.g. ``/etc``
#: is a symlink to ``/private/etc`` on macOS).
SENSITIVE_READ_DENY_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.config",
    "~/.gnupg",
    "~/.minimax",
    "~/Library/Keychains",
    "~/Library/Application Support",
    "/etc/ssh",
    "/private/etc/ssh",
)

_PROC_PIDTASKINFO = 4  # proc flavor number on Darwin
_PROC_TASKINFO_BUFFER = ctypes.create_string_buffer(104)  # >= sizeof(struct proc_taskinfo)


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits applied to one sandboxed run.

    All values must be positive. Defaults match the cross-task contract:
    ``max_seconds=60, max_output_bytes=65536, max_memory_mb=512,
    max_cpu_seconds=60, max_processes=8``.
    """

    max_seconds: float = 60
    max_output_bytes: int = 65536
    max_memory_mb: int = 512
    max_cpu_seconds: int = 60
    max_processes: int = 8

    def __post_init__(self) -> None:
        for name in (
            "max_seconds",
            "max_output_bytes",
            "max_memory_mb",
            "max_cpu_seconds",
            "max_processes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"SandboxLimits.{name} must be > 0, got {getattr(self, name)}")


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of one sandboxed run (fields fixed by the cross-task contract).

    ``stdout``/``stderr`` are raw bytes, each truncated to
    ``limits.max_output_bytes``. ``truncated`` marks that any output stream
    was cut or that a collectible file was skipped for exceeding the 256 KB
    collection cap (details are appended to ``stderr`` as
    ``[p2r_sandbox]`` lines). ``files`` maps each successfully collected
    whitelist file name to its bytes. ``duration_ms`` covers process start
    through file collection. ``backend`` names the isolation backend used
    for this run: ``"seatbelt"`` (macOS), ``"bwrap"`` / ``"unshare"``
    (Linux, probe-gated) or ``"none"`` (no network/write isolation
    available).
    """

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    network_isolated: bool
    files: dict[str, bytes]
    duration_ms: int
    #: Isolation backend actually used. Defaults to "none" so that callers
    #: constructing results field-by-field (or positionally) keep working.
    backend: str = "none"


# --------------------------------------------------------------------------
# seatbelt
# --------------------------------------------------------------------------


def _seatbelt_profile(allowed_dir: str) -> str:
    """Seatbelt profile: deny network, deny sensitive reads, deny writes
    except ``allowed_dir``.

    ``allowed_dir`` MUST be a real path (symlinks resolved): the seatbelt
    kernel extension matches resolved paths, so a ``/tmp/...`` literal never
    matches the ``/private/tmp/...`` the guest actually opens (verified
    empirically on this host).

    Sensitive read denials are prefix rules over well-known secret locations
    plus a ``regex`` for ``~/.env*`` files in the home directory. In
    seatbelt profiles the last matching rule wins; the sensitive ``deny
    file-read*`` rules come after ``(allow default)`` and are never
    re-allowed, so they take effect (verified live by the probe: listing
    ``~/Library/Keychains`` must fail with ``EPERM``).
    """
    home = os.path.expanduser("~")
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{allowed_dir}"))',
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/dtrace.helper"))',
        '(allow file-write* (literal "/dev/dtrace.worker"))',
    ]
    for raw_path in SENSITIVE_READ_DENY_PATHS:
        path = os.path.expanduser(raw_path)
        lines.append(f'(deny file-read* (subpath "{path}"))')
    # ~/.env* in the home directory (e.g. .env, .env.local): seatbelt has no
    # glob syntax for file-read, so a prefix regex is used. re.escape keeps
    # unusual home-directory spellings literal for the ICU regex engine.
    lines.append(f'(deny file-read* (regex #"^{re.escape(home)}/\\.env"))')
    return "\n".join(lines)


_SEATBELT_PROBE_SCRIPT = r"""
import errno, json, os, socket, sys

results = {"net": False, "write": False, "sensitive": False}
try:
    socket.create_connection(("127.0.0.1", 1), timeout=2).close()
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        results["net"] = True  # sandbox said "no"; ECONNREFUSED means net works
try:
    with open(sys.argv[1], "w") as fh:
        fh.write("x")
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        results["write"] = True
try:
    os.listdir(sys.argv[2])
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        results["sensitive"] = True
print(json.dumps(results))
sys.exit(0)
"""

#: Cached per-check seatbelt probe results ({"net", "write", "sensitive"}).
_seatbelt_cache: dict[str, bool] | None = None


def _sensitive_probe_path() -> str:
    """First *existing* sensitive directory usable for a live read probe.

    ``~/Library/Keychains`` exists on every macOS host, so the probe can
    prove denial with a real ``EPERM`` instead of guessing about paths that
    may not exist (a missing path yields ``ENOENT`` regardless of policy).
    """
    for raw_path in SENSITIVE_READ_DENY_PATHS:
        path = os.path.expanduser(raw_path)
        if os.path.isdir(path):
            return path
    return os.path.expanduser("~/Library/Keychains")


def _probe_seatbelt() -> dict[str, bool]:
    """Live-probe the seatbelt profile with one child process.

    The child must demonstrate, under the profile: network connect denied,
    outside write denied and a sensitive directory listing denied - each
    reported as an independent boolean. The child also imports stdlib
    modules implicitly (it needs ``json``/``socket`` to report), so a
    profile that broke interpreter/stdlib reads fails the probe entirely.
    Any launch or output problem degrades every check to False: we never
    claim a capability we could not demonstrate on this host.
    """
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return {"net": False, "write": False, "sensitive": False}
    workdir = tempfile.mkdtemp(prefix="p2r_sbx_probe_")
    outside_probe = os.path.join(
        tempfile.gettempdir(), f"p2r_sbx_probe_outside_{os.getpid()}"
    )
    try:
        real = os.path.realpath(workdir)
        script = os.path.join(workdir, "probe.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_SEATBELT_PROBE_SCRIPT)
        try:
            proc = subprocess.run(
                [
                    SANDBOX_EXEC,
                    "-p",
                    _seatbelt_profile(real),
                    sys.executable,
                    "-I",
                    "probe.py",
                    outside_probe,
                    _sensitive_probe_path(),
                ],
                cwd=workdir,
                env={"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return {"net": False, "write": False, "sensitive": False}
        try:
            data = json.loads(proc.stdout.decode("utf-8"))
            return {
                "net": bool(data.get("net")),
                "write": bool(data.get("write")),
                "sensitive": bool(data.get("sensitive")),
            }
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return {"net": False, "write": False, "sensitive": False}
    finally:
        # If the profile somehow failed to enforce, the probe child managed
        # to create this file - remove the artifact either way.
        try:
            os.unlink(outside_probe)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _seatbelt_probe_result() -> dict[str, bool]:
    global _seatbelt_cache
    if _seatbelt_cache is None:
        _seatbelt_cache = _probe_seatbelt()
    return _seatbelt_cache


def _seatbelt_available() -> bool:
    """True iff a live child under the profile proved net+write denial."""
    checks = _seatbelt_probe_result()
    return checks["net"] and checks["write"]


# --------------------------------------------------------------------------
# Linux backends: bubblewrap, then unshare -rn (probe-gated, honest)
# --------------------------------------------------------------------------


#: Shared probe for namespace-based backends. ``net`` is True only when both
#: connect attempts fail with an errno that proves packets cannot leave (a
#: fresh network namespace has loopback DOWN -> ``ENETUNREACH``; policy
#: denials surface as ``EPERM``/``EACCES``). ``ECONNREFUSED`` would mean the
#: loopback route worked, i.e. no isolation, and is rejected. ``write`` is
#: None (not checked) or the denied/allowed verdict for a file outside the
#: sandbox island.
_NS_PROBE_SCRIPT = r"""
import errno, json, socket, sys

results = {"net": False}
net_errnos = (errno.EPERM, errno.EACCES, errno.ENETUNREACH, errno.ENETDOWN)
denied = 0
for host, port in (("127.0.0.1", 1), ("192.0.2.1", 1)):
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError as exc:
        if exc.errno in net_errnos:
            denied += 1
    except OSError:
        pass
results["net"] = denied == 2
if len(sys.argv) > 1:
    results["write"] = True
    try:
        with open(sys.argv[1], "w") as fh:
            fh.write("x")
        results["write"] = False
    except OSError:
        pass
print(json.dumps(results))
sys.exit(0)
"""


def _bwrap_argv(base_argv: Sequence[str], workdir_real: str) -> list[str]:
    """argv to run ``base_argv`` under bubblewrap (pure, no probing).

    Mount policy: read-only view of the whole host filesystem, a private
    ``/tmp``, fresh ``/dev`` and ``/proc``, and the sandbox workdir re-bound
    writable. Mount order matters: the workdir bind is applied AFTER the
    ``/tmp`` tmpfs so a workdir under ``/tmp`` is not masked by it.
    ``--unshare-net`` removes the network namespace; ``--die-with-parent``
    reaps the tree if the supervisor dies. Reads outside the workdir stay
    possible by design (documented limitation, matches the seatbelt threat
    model minus sensitive-path denies).
    """
    return [
        "bwrap",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        workdir_real,
        workdir_real,
        "--die-with-parent",
        "--",
        *base_argv,
    ]


def _unshare_argv(base_argv: Sequence[str]) -> list[str]:
    """argv to run ``base_argv`` under ``unshare -rn`` (pure, no probing).

    ``-r`` maps the current uid to root inside a fresh user namespace and
    ``-n`` creates an isolated network namespace. Filesystem writes are NOT
    restricted by this backend - callers must not claim write isolation.
    """
    return ["unshare", "-rn", *base_argv]


def _wrapped_argv(backend: str, python: str, workdir_real: str) -> list[str]:
    """Full launch argv for ``backend`` (pure; tested without Linux)."""
    base = [python, "-I", "script.py"]
    if backend == "seatbelt":
        return [SANDBOX_EXEC, "-p", _seatbelt_profile(workdir_real)] + base
    if backend == "bwrap":
        return _bwrap_argv(base, workdir_real)
    if backend == "unshare":
        return _unshare_argv(base)
    return base


def _run_ns_probe(argv: list[str], workdir: str, outside_probe: str | None) -> dict:
    """Run one backend probe child; parse its per-check JSON verdict."""
    script = os.path.join(workdir, "probe.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_NS_PROBE_SCRIPT)
    child_argv = argv + (["probe.py", outside_probe] if outside_probe else ["probe.py"])
    try:
        proc = subprocess.run(
            child_argv,
            cwd=workdir,
            env={"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


_bwrap_cache: bool | None = None
_unshare_cache: bool | None = None


def _probe_bwrap() -> bool:
    """True iff bwrap exists AND a live child proves net + outside-write denial."""
    if shutil.which("bwrap") is None or sys.platform == "win32":
        return False
    workdir = tempfile.mkdtemp(prefix="p2r_bwx_probe_")
    # /tmp is shadowed by the private tmpfs inside bwrap, so the write probe
    # targets /var/tmp: read-only under the mount policy, writable on a host
    # where bwrap failed to apply it (probe then honestly fails).
    outside_probe = f"/var/tmp/p2r_bwx_probe_outside_{os.getpid()}"
    try:
        checks = _run_ns_probe(
            _bwrap_argv([sys.executable, "-I"], os.path.realpath(workdir)),
            workdir,
            outside_probe,
        )
        return bool(checks.get("net")) and bool(checks.get("write"))
    finally:
        try:
            os.unlink(outside_probe)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _probe_unshare() -> bool:
    """True iff ``unshare -rn`` exists AND a live child proves net denial.

    Write isolation is intentionally NOT probed (this backend cannot
    provide it); enabling unshare only claims network isolation.
    """
    if shutil.which("unshare") is None or sys.platform == "win32":
        return False
    workdir = tempfile.mkdtemp(prefix="p2r_uns_probe_")
    try:
        checks = _run_ns_probe(_unshare_argv([sys.executable, "-I"]), workdir, None)
        return bool(checks.get("net"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _bwrap_available() -> bool:
    global _bwrap_cache
    if _bwrap_cache is None:
        _bwrap_cache = _probe_bwrap()
    return _bwrap_cache


def _unshare_net_available() -> bool:
    global _unshare_cache
    if _unshare_cache is None:
        _unshare_cache = _probe_unshare()
    return _unshare_cache


def _select_backend() -> tuple[str, bool]:
    """Pick the strongest verified backend: ``(backend, network_isolated)``.

    Preference order: macOS seatbelt, bubblewrap, ``unshare -rn``; "none"
    means no network/write isolation could be verified and the caller gets
    ``network_isolated=False``. unshare provides network isolation only -
    the write gap is reported via a stderr note by the executor.
    """
    if sys.platform == "win32":
        return "none", False
    if sys.platform == "darwin" and _seatbelt_available():
        return "seatbelt", True
    if _bwrap_available():
        return "bwrap", True
    if _unshare_net_available():
        return "unshare", True
    return "none", False


# --------------------------------------------------------------------------
# rlimit support detection
# --------------------------------------------------------------------------

_rlimit_support_cache: dict[str, bool] | None = None

_RLIMIT_PROBE_CODE = """
import json, resource

def try_set(name, value):
    res = getattr(resource, name)
    try:
        resource.setrlimit(res, (value, value))
        cur, _hard = resource.getrlimit(res)
        return cur == value
    except Exception:
        return False

print(json.dumps({
    "as": try_set("RLIMIT_AS", 64 * 1024 * 1024),
    "cpu": try_set("RLIMIT_CPU", 60),
    "nproc": try_set("RLIMIT_NPROC", 8),
    "fsize": try_set("RLIMIT_FSIZE", 4 * 1024 * 1024),
}))
"""


def _probe_rlimits() -> dict[str, bool]:
    """Which rlimits a child process can actually set on this platform.

    The probe child applies exactly the calls the real preexec will make and
    verifies via ``getrlimit`` that they stuck. On this macOS host RLIMIT_AS
    is rejected outright (EINVAL for any finite value); CPU/NPROC/FSIZE work.
    """
    if sys.platform == "win32" or not hasattr(resource, "setrlimit"):
        return {"as": False, "cpu": False, "nproc": False, "fsize": False}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RLIMIT_PROBE_CODE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        data = json.loads(proc.stdout.decode("utf-8"))
        return {
            key: bool(data.get(key))
            for key in ("as", "cpu", "nproc", "fsize")
        }
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, json.JSONDecodeError):
        return {"as": False, "cpu": False, "nproc": False, "fsize": False}


def _rlimit_support() -> dict[str, bool]:
    global _rlimit_support_cache
    if _rlimit_support_cache is None:
        _rlimit_support_cache = _probe_rlimits()
    return _rlimit_support_cache


def sandbox_capabilities() -> dict[str, bool]:
    """Report which isolation layers this host actually provides.

    Returns at least ``{"seatbelt": bool, "rlimits": bool,
    "platform_walls": bool}`` plus additive keys introduced later
    (``sensitive_read_denied``, ``bubblewrap``, ``unshare_net``); consumers
    must treat unknown keys as additive, never as a fixed schema. Values
    come from live probes (cached per process): ``seatbelt`` is True only
    when a child under the seatbelt profile was verifiably denied network
    and outside writes; ``sensitive_read_denied`` is True only when the same
    probe child verifiably failed to list an existing sensitive directory
    (``EPERM``/``EACCES``); ``rlimits`` is True when at least one resource
    limit could be applied in a child; ``platform_walls`` marks the Windows
    path where walls are wall clock, output caps, a throwaway cwd and a
    direct child kill - with no network, memory or process-count
    enforcement; ``bubblewrap`` / ``unshare_net`` report the Linux backend
    probes (always False where the binary is missing, e.g. macOS).
    """
    support = _rlimit_support()
    seatbelt_checks = _seatbelt_probe_result()
    return {
        "seatbelt": seatbelt_checks["net"] and seatbelt_checks["write"],
        "sensitive_read_denied": seatbelt_checks["sensitive"],
        "platform_walls": sys.platform == "win32",
        "rlimits": any(support.values()),
        "bubblewrap": _bwrap_available(),
        "unshare_net": _unshare_net_available(),
    }


# --------------------------------------------------------------------------
# child RSS monitoring (memory limit fallback where RLIMIT_AS is unusable)
# --------------------------------------------------------------------------

_libc: ctypes.CDLL | None = None


def _rss_bytes(pid: int) -> int | None:
    """Resident memory of ``pid`` in bytes, or None when unavailable."""
    if sys.platform == "darwin":
        global _libc
        try:
            if _libc is None:
                _libc = ctypes.CDLL(None, use_errno=True)
                _libc.proc_pidinfo.restype = ctypes.c_int
            buf = ctypes.create_string_buffer(104)
            filled = _libc.proc_pidinfo(
                pid, _PROC_PIDTASKINFO, 0, buf, ctypes.sizeof(buf)
            )
            if filled <= 0:
                return None
            _virtual, resident = struct.unpack_from("QQ", buf, 0)
            return int(resident)
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/statm", "rb") as fh:
                fields = fh.read().split()
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    return None


_ps_rss_cache: str | None = None


def _ps_executable() -> str | None:
    """Path to a usable ``ps`` binary, or None (cached)."""
    global _ps_rss_cache
    if _ps_rss_cache is None:
        _ps_rss_cache = shutil.which("ps") or ""
    return _ps_rss_cache or None


def _ps_group_rss_bytes(pgid: int) -> int | None:
    """Summed RSS of every process in ``pgid`` via ``ps`` (literal argv).

    ``ps -o pid=,rss= -g <pgid>`` lists each group member's resident size in
    KiB on both macOS (BSD ps) and Linux (procps). Returns None when ``ps``
    is unavailable, fails, or the group is already gone - callers then fall
    back to the group leader.
    """
    ps = _ps_executable()
    if ps is None:
        return None
    try:
        proc = subprocess.run(
            [ps, "-o", "pid=,rss=", "-g", str(pgid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    total = 0
    try:
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            if fields:
                total += int(fields[-1]) * 1024
    except ValueError:
        return None
    return total


def _procfs_group_rss_bytes(pgid: int) -> int | None:
    """Summed RSS of every process in ``pgid`` by scanning /proc (Linux).

    Returns None when /proc itself is unreadable; returns 0 when the group
    is simply empty (everything already exited).
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
            # pgrp is field 5; comm (field 2) may contain spaces/parens, so
            # split after the LAST ')' and index the remaining fields:
            # [state, ppid, pgrp, ...].
            after_comm = data[data.rindex(b")") + 1:].split()
            if int(after_comm[2]) != pgid:
                continue
            with open(f"/proc/{entry}/statm", "rb") as fh:
                resident_pages = int(fh.read().split()[1])
            total += resident_pages * page
        except (OSError, ValueError, IndexError):
            # Raced with process exit; undercount slightly, never crash.
            continue
    return total


def _group_rss_bytes(pgid: int) -> int | None:
    """Resident memory summed over the guest's whole process tree, bytes.

    The guest leader runs in its own process group (``start_new_session``),
    so group membership covers every descendant it forked - the basis for
    tree-wide memory enforcement. Returns None when no enumeration source
    exists on this platform (Windows), letting the monitor fall back to the
    leader-only measurement (and, there, to no memory enforcement at all).
    """
    if sys.platform == "darwin":
        return _ps_group_rss_bytes(pgid)
    if sys.platform.startswith("linux"):
        total = _procfs_group_rss_bytes(pgid)
        if total is not None:
            return total
        return _ps_group_rss_bytes(pgid)
    return None


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------


def _guest_env(python_exe: str, workdir: str) -> dict[str, str]:
    """Whitelist-constructed guest environment (never a copy of the host's).

    Only PATH and TMPDIR are set. TMPDIR points inside the sandbox workdir so
    that a guest using ``tempfile`` stays within its writable island.
    """
    path_entries = [
        os.path.dirname(os.path.abspath(python_exe)),
        "/usr/bin",
        "/bin",
        os.path.expanduser("~/usr/bin"),
    ]
    return {"PATH": os.pathsep.join(path_entries), "TMPDIR": workdir}


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the guest's whole process group (child is the group leader).

    Windows has no process groups via start_new_session; the guest is a
    single ``python -I`` process there, so killing the child directly is the
    same enforcement scope. A guest that spawns its own children can escape
    this on Windows - recorded honestly via ``platform_walls`` capability.
    """
    if sys.platform == "win32":
        try:
            proc.kill()
        except OSError:
            pass
        return
    if not _sweep_process_group(proc.pid):
        try:
            proc.kill()
        except OSError:
            pass


def _sweep_process_group(pgid: int, attempts: int = 200) -> bool:
    """SIGKILL ``pgid`` repeatedly until the group is empty.

    A single ``killpg`` races with a guest that is forking at that instant:
    the new child joins the group after the signal was delivered and
    survives (observed with a fork bomb). Re-signalling until the kernel
    reports ESRCH closes that window. Also used after a *normal* exit so a
    guest cannot leave background children behind in its group.
    Returns False only if the group could not be signalled at all.
    """
    signalled = False
    for _ in range(attempts):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return signalled
        signalled = True
        time.sleep(0.005)
    return signalled


def _drain_pipe(fd: int, buffer: bytearray, cap: int, truncated: bool) -> bool:
    """Non-blocking drain of already-buffered pipe bytes; returns truncated."""
    while True:
        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            return truncated
        chunk = os.read(fd, 65536)
        if not chunk:
            return truncated
        room = cap - len(buffer)
        if room > 0:
            buffer.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            truncated = True


def _pump(
    proc: subprocess.Popen, limits: SandboxLimits
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Read guest stdout/stderr to the caps, enforce wall clock + memory.

    Returns ``(stdout, stderr, timed_out, truncated, memory_killed,
    memory_rss_observed)``; the last element is the tree-wide RSS total that
    tripped the memory limit (None otherwise). Memory is measured over the
    whole process group, so memory hoarded by guest *children* counts too.
    Reading is incremental so output is capped in memory as it arrives and
    the child never blocks on a full pipe we refuse to store.
    """
    selector = selectors.DefaultSelector()
    buffers = {"out": bytearray(), "err": bytearray()}
    cap = limits.max_output_bytes
    streams = {"out": proc.stdout, "err": proc.stderr}
    for name, stream in streams.items():
        selector.register(stream, selectors.EVENT_READ, name)
    truncated = False
    timed_out = False
    memory_killed = False
    memory_rss_observed: int | None = None
    deadline = time.monotonic() + limits.max_seconds
    memory_limit_bytes = limits.max_memory_mb * 1024 * 1024
    # RSS sampling is deadline-scheduled (not tied to output events): the
    # leader read is cheap and frequent, the whole-tree sweep is expensive
    # and rarer. select() is capped to the next due sample so a silent guest
    # cannot delay enforcement.
    now = time.monotonic()
    next_leader_sample = now + _LEADER_RSS_INTERVAL_SECONDS
    next_tree_sample = now + _TREE_RSS_INTERVAL_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            now = time.monotonic()
            next_sample = min(next_leader_sample, next_tree_sample)
            timeout = min(remaining, _POLL_INTERVAL_SECONDS, max(next_sample - now, 0))
            events = selector.select(timeout=timeout)
            for key, _mask in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer = buffers[key.data]
                room = cap - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
            rss: int | None = None
            now = time.monotonic()
            if now >= next_leader_sample:
                rss = _rss_bytes(proc.pid)
                next_leader_sample = now + _LEADER_RSS_INTERVAL_SECONDS
            if now >= next_tree_sample:
                # Whole-group total covers memory hoarded by guest children.
                tree_rss = _group_rss_bytes(proc.pid)
                if tree_rss is not None:
                    rss = max(tree_rss, rss or 0)
                next_tree_sample = now + _TREE_RSS_INTERVAL_SECONDS
            if rss is not None and rss > memory_limit_bytes:
                memory_killed = True
                memory_rss_observed = rss
                break
    finally:
        if timed_out or memory_killed:
            _kill_process_group(proc)
        registered_names = {
            key.data for key in selector.get_map().values()
        } if selector.get_map() else set()
        for name, stream in streams.items():
            try:
                if name in registered_names:
                    selector.unregister(stream)
            except (KeyError, ValueError):
                pass
            if not stream.closed:
                try:
                    truncated = _drain_pipe(
                        stream.fileno(), buffers[name], cap, truncated
                    )
                except (OSError, ValueError):
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
        selector.close()
    proc.wait()
    return (
        bytes(buffers["out"]),
        bytes(buffers["err"]),
        timed_out,
        truncated,
        memory_killed,
        memory_rss_observed,
    )



def _pump_windows(
    proc: subprocess.Popen, limits: SandboxLimits
) -> tuple[bytes, bytes, bool, bool]:
    """Windows reader: select() cannot watch pipes there, so two daemon
    threads drain stdout/stderr under the same byte caps while the main loop
    enforces the wall clock. Returns ``(stdout, stderr, timed_out, truncated)``.
    """
    cap = limits.max_output_bytes
    buffers = {"out": bytearray(), "err": bytearray()}
    truncated = {"value": False}

    def drain(name: str, stream) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            buffer = buffers[name]
            room = cap - len(buffer)
            if room > 0:
                buffer.extend(chunk[:room])
                if len(chunk) > room:
                    truncated["value"] = True
            else:
                truncated["value"] = True

    threads = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (("out", proc.stdout), ("err", proc.stderr))
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    deadline = time.monotonic() + limits.max_seconds
    try:
        while any(thread.is_alive() for thread in threads):
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        for thread in threads:
            thread.join(timeout=1.0)
        for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
            try:
                if not stream.closed:
                    stream.close()
            except OSError:
                pass
    proc.wait()
    return bytes(buffers["out"]), bytes(buffers["err"]), timed_out, truncated["value"]


def _collect_one_file(
    workdir: str, real_workdir: str, name: str, collected: dict[str, bytes], notes: list[str]
) -> None:
    """Collect one exact workdir-relative file into ``collected``.

    Symlink/path escapes resolve to a realpath outside ``real_workdir`` and
    are refused with a note; missing files are not an error (the caller
    validates presence via ``validate_output_files``).
    """
    candidate = os.path.realpath(os.path.join(workdir, name))
    if candidate != real_workdir and not candidate.startswith(real_workdir + os.sep):
        notes.append(f"collect path escapes workdir, skipped: {name}")
        return
    try:
        if not os.path.isfile(candidate):
            return
        if os.path.getsize(candidate) > MAX_COLLECT_FILE_BYTES:
            notes.append(
                f"file exceeds {MAX_COLLECT_FILE_BYTES} byte collection cap, "
                f"not collected: {name}"
            )
            return
        with open(candidate, "rb") as fh:
            collected[name] = fh.read()
    except OSError as exc:
        notes.append(f"file unreadable, not collected: {name} ({exc})")


def _collect_files(
    workdir: str, names: Iterable[str]
) -> tuple[dict[str, bytes], list[str]]:
    """Collect whitelist files from workdir (each <= 256 KB).

    Entries containing glob characters (``*``, ``?``, ``[``) are matched
    with :mod:`fnmatch` against the workdir's top-level entries; plain
    entries are collected by exact name. Returns ``(files, notes)`` where
    notes are human-readable reasons for skipped files (oversize, path
    escape, unreadable). Missing files / patterns without matches are not
    an error here - the caller validates presence via
    ``validate_output_files``.
    """
    collected: dict[str, bytes] = {}
    notes: list[str] = []
    real_workdir = os.path.realpath(workdir)
    try:
        entries = os.listdir(workdir)
    except OSError:
        entries = []
    for name in names:
        if any(ch in name for ch in "*?["):
            matched = sorted(e for e in entries if fnmatch.fnmatch(e, name))
            for entry in matched:
                _collect_one_file(workdir, real_workdir, entry, collected, notes)
            continue
        _collect_one_file(workdir, real_workdir, name, collected, notes)
    return collected, notes


def run_python_sandbox(
    code: str,
    *,
    limits: SandboxLimits | None = None,
    python_exe: str | None = None,
    collect_files: Sequence[str] = ("predictions.json", "labels.json"),
) -> SandboxResult:
    """Run ``code`` inside the host-isolation stack and collect its outputs.

    See the module docstring for the exact isolation layers. Raises
    :class:`SandboxUnavailable` when the child cannot be launched or the
    preexec resource-limit setup fails; guest-side failures are reported via
    the returned :class:`SandboxResult`.
    """
    limits = limits if limits is not None else SandboxLimits()
    python = python_exe if python_exe is not None else sys.executable
    workdir = tempfile.mkdtemp(prefix="p2r_sbx_")
    notes: list[str] = []
    try:
        script_path = os.path.join(workdir, "script.py")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(code)

        backend, network_isolated = _select_backend()
        workdir_real = os.path.realpath(workdir)
        argv = _wrapped_argv(backend, python, workdir_real)
        if backend == "unshare":
            # Honest gap: unshare namespaces the network but leaves the
            # filesystem writable - never present this as a full sandbox.
            notes.append(
                "backend 'unshare' provides network-only isolation; "
                "writes outside the workdir are NOT restricted"
            )

        rlimit_ok = _rlimit_support()
        if not rlimit_ok["as"]:
            notes.append(
                "RLIMIT_AS rejected by this platform; memory enforced by "
                "RSS monitor + process-group kill"
            )

        def preexec() -> None:  # runs in the forked child before exec
            if rlimit_ok["as"]:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (limits.max_memory_mb * 1024 * 1024,) * 2,
                )
            if rlimit_ok["cpu"]:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (limits.max_cpu_seconds,) * 2
                )
            if rlimit_ok["nproc"]:
                resource.setrlimit(
                    resource.RLIMIT_NPROC, (limits.max_processes,) * 2
                )
            if rlimit_ok["fsize"]:
                resource.setrlimit(resource.RLIMIT_FSIZE, (RLIMIT_FSIZE_BYTES,) * 2)

        started = time.monotonic()
        try:
            spawn_kwargs: dict = {}
            if sys.platform == "win32":
                # No fork/setsid/rlimits on Windows: walls are wall clock,
                # output caps, the throwaway cwd and direct child kill.
                spawn_kwargs = {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW,
                }
            else:
                spawn_kwargs = {"start_new_session": True, "preexec_fn": preexec}
            proc = subprocess.Popen(
                argv,
                cwd=workdir,
                env=_guest_env(python, workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **spawn_kwargs,
            )
        except subprocess.SubprocessError as exc:  # includes preexec failures
            raise SandboxUnavailable(
                f"failed to launch sandboxed python: {exc}"
            ) from exc
        except OSError as exc:
            raise SandboxUnavailable(
                f"failed to launch sandboxed python: {exc}"
            ) from exc
        except ValueError as exc:
            # e.g. preexec_fn is unsupported on this platform (Windows)
            raise SandboxUnavailable(
                f"sandbox launch not supported on this platform: {exc}"
            ) from exc

        if sys.platform == "win32":
            stdout, stderr, timed_out, truncated = _pump_windows(proc, limits)
            memory_killed = False
            memory_rss_observed = None
        else:
            (
                stdout,
                stderr,
                timed_out,
                truncated,
                memory_killed,
                memory_rss_observed,
            ) = _pump(proc, limits)
        exit_code = proc.returncode
        if sys.platform != "win32":
            # The leader has exited; reap anything it left in its group.
            _sweep_process_group(proc.pid)
        files, file_notes = _collect_files(workdir, collect_files)
        duration_ms = int((time.monotonic() - started) * 1000)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    notes.extend(file_notes)
    if memory_killed:
        observed = (
            f", observed {memory_rss_observed / (1024 * 1024):.0f} MiB across "
            "the process tree"
            if memory_rss_observed is not None
            else ""
        )
        notes.append(
            f"memory limit exceeded ({limits.max_memory_mb} MiB RSS{observed}): "
            "process group killed"
        )
    if timed_out:
        notes.append(f"wall-clock limit exceeded ({limits.max_seconds}s): killed")
    if notes:
        marker = "\n".join(f"[p2r_sandbox] {note}" for note in notes)
        stderr = stderr + marker.encode("utf-8")
        if len(stderr) > limits.max_output_bytes:
            stderr = stderr[: limits.max_output_bytes]
            truncated = True
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=truncated or bool(file_notes),
        network_isolated=network_isolated,
        files=files,
        duration_ms=duration_ms,
        backend=backend,
    )
