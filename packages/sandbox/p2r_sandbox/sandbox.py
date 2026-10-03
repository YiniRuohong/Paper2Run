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
   instead enforced by an RSS monitor (``proc_pidinfo`` on Darwin,
   ``/proc/<pid>/statm`` on Linux) that kills the whole process group with
   ``SIGKILL`` once resident memory exceeds ``max_memory_mb``. This
   substitution is recorded in ``SandboxResult.stderr`` - never silent.
6. **Wall-clock timeout**: output is pumped incrementally with a deadline;
   on expiry the whole process group gets ``SIGKILL`` and the result is
   marked ``timed_out=True``.
7. **Output caps**: stdout/stderr are each truncated to
   ``max_output_bytes`` while being read (so a guest flooding stdout cannot
   exhaust host memory), with ``truncated=True`` marking.
8. **macOS seatbelt** (``/usr/bin/sandbox-exec``) when available and
   *verified effective*: a profile that denies all ``network*`` operations
   and all ``file-write*`` operations except inside the (real-path) sandbox
   workdir plus ``/dev/null``. Reads stay allowed everywhere. Effectiveness
   is proven by a one-time probe (a child under the profile must get
   ``EPERM``/``EACCES`` when connecting to ``127.0.0.1:1`` and when writing
   outside the workdir). If ``sandbox-exec`` is missing or the probe fails,
   the run still happens but ``network_isolated=False`` and
   ``sandbox_capabilities()["seatbelt"]`` is ``False`` - we never pretend to
   isolate when we cannot.

No shell is ever involved: ``Popen`` receives literal argument lists only.
"""

from __future__ import annotations

import ctypes
import json
import os
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
#: macOS injects these into every process at startup (CoreFoundation); they
#: are not inherited from the host environment and carry no secrets.
_PLATFORM_INJECTED_ENV_KEYS = frozenset({"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})

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
    through file collection.
    """

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    network_isolated: bool
    files: dict[str, bytes]
    duration_ms: int


# --------------------------------------------------------------------------
# seatbelt
# --------------------------------------------------------------------------


def _seatbelt_profile(allowed_dir: str) -> str:
    """Seatbelt profile: deny all network, deny writes except ``allowed_dir``.

    ``allowed_dir`` MUST be a real path (symlinks resolved): the seatbelt
    kernel extension matches resolved paths, so a ``/tmp/...`` literal never
    matches the ``/private/tmp/...`` the guest actually opens (verified
    empirically on this host).
    """
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{allowed_dir}"))',
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-write* (literal "/dev/dtrace.helper"))',
            '(allow file-write* (literal "/dev/dtrace.worker"))',
        ]
    )


_SEATBELT_PROBE_SCRIPT = r"""
import errno, socket, sys

denied = 0
try:
    socket.create_connection(("127.0.0.1", 1), timeout=2).close()
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        denied += 1  # sandbox said "no"; ECONNREFUSED would mean network works
try:
    with open(sys.argv[1], "w") as fh:
        fh.write("x")
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        denied += 1
sys.exit(0 if denied == 2 else 3)
"""

_seatbelt_cache: bool | None = None


def _probe_seatbelt() -> bool:
    """True iff sandbox-exec exists AND a live child proves net+write denial."""
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return False
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
                ],
                cwd=workdir,
                env={"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0
    finally:
        # If the profile somehow failed to enforce, the probe child managed
        # to create this file - remove the artifact either way.
        try:
            os.unlink(outside_probe)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _seatbelt_available() -> bool:
    global _seatbelt_cache
    if _seatbelt_cache is None:
        _seatbelt_cache = _probe_seatbelt()
    return _seatbelt_cache


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

    Returns ``{"seatbelt": bool, "rlimits": bool, "platform_walls": bool}``.
    Values come from live probes (cached per process): ``seatbelt`` is True
    only when a child under the seatbelt profile was verifiably denied network
    and outside writes; ``rlimits`` is True when at least one resource limit
    could be applied in a child; ``platform_walls`` marks the Windows path
    where walls are wall clock, output caps, a throwaway cwd and a direct
    child kill - with no network, memory or process-count enforcement.
    """
    support = _rlimit_support()
    return {
        "seatbelt": _seatbelt_available(),
        "platform_walls": sys.platform == "win32",
        "rlimits": any(support.values()),
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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


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

    Returns ``(stdout, stderr, timed_out, truncated, memory_killed)``.
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
    deadline = time.monotonic() + limits.max_seconds
    memory_limit_bytes = limits.max_memory_mb * 1024 * 1024
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(remaining, _POLL_INTERVAL_SECONDS))
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
            rss = _rss_bytes(proc.pid)
            if rss is not None and rss > memory_limit_bytes:
                memory_killed = True
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
    return bytes(buffers["out"]), bytes(buffers["err"]), timed_out, truncated, memory_killed



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


def _collect_files(
    workdir: str, names: Iterable[str]
) -> tuple[dict[str, bytes], list[str]]:
    """Collect whitelist files from workdir (each <= 256 KB).

    Returns ``(files, notes)`` where notes are human-readable reasons for
    skipped files (oversize, path escape, unreadable). Missing files are not
    an error here - the caller validates presence via
    ``validate_output_files``.
    """
    collected: dict[str, bytes] = {}
    notes: list[str] = []
    real_workdir = os.path.realpath(workdir)
    for name in names:
        candidate = os.path.realpath(os.path.join(workdir, name))
        if candidate != real_workdir and not candidate.startswith(
            real_workdir + os.sep
        ):
            notes.append(f"collect path escapes workdir, skipped: {name}")
            continue
        try:
            if not os.path.isfile(candidate):
                continue
            if os.path.getsize(candidate) > MAX_COLLECT_FILE_BYTES:
                notes.append(
                    f"file exceeds {MAX_COLLECT_FILE_BYTES} byte collection cap, "
                    f"not collected: {name}"
                )
                continue
            with open(candidate, "rb") as fh:
                collected[name] = fh.read()
        except OSError as exc:
            notes.append(f"file unreadable, not collected: {name} ({exc})")
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

        use_seatbelt = _seatbelt_available()
        argv = [python, "-I", "script.py"]
        if use_seatbelt:
            argv = [SANDBOX_EXEC, "-p", _seatbelt_profile(os.path.realpath(workdir))] + argv

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
        else:
            stdout, stderr, timed_out, truncated, memory_killed = _pump(proc, limits)
        exit_code = proc.returncode
        files, file_notes = _collect_files(workdir, collect_files)
        duration_ms = int((time.monotonic() - started) * 1000)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    notes.extend(file_notes)
    if memory_killed:
        notes.append(
            f"memory limit exceeded ({limits.max_memory_mb} MiB RSS): "
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
        network_isolated=use_seatbelt,
        files=files,
        duration_ms=duration_ms,
    )
